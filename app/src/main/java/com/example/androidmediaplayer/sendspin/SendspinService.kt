package com.example.androidmediaplayer.sendspin

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import com.example.androidmediaplayer.util.AppLog
import io.ktor.server.application.*
import io.ktor.server.engine.*
import io.ktor.server.netty.*
import io.ktor.server.routing.*
import io.ktor.server.websocket.*
import io.ktor.websocket.*
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.time.Duration
import java.util.UUID

/**
 * Sendspin service managing mDNS advertisement and WebSocket server.
 *
 * Advertises as a Sendspin player via mDNS and accepts WebSocket connections
 * from Sendspin servers (e.g., Music Assistant).
 */
class SendspinService(
    private val context: Context,
    private var deviceName: String,
    private val onStreamStart: (SendspinStreamConfig) -> Unit,
    private val onStreamEnd: () -> Unit,
    private val onVolumeChange: (Int) -> Unit,
    private val onMuteChange: (Boolean) -> Unit
) {
    companion object {
        private const val TAG = "SendspinService"
        private const val PREFS_NAME = "sendspin_prefs"
        private const val KEY_CLIENT_ID = "client_id"
    }

    // Persistent client ID for this device (survives app restarts)
    private val clientId: String = getOrCreateClientId()

    // Components
    private val clockSync = SendspinClockSync()
    val audioBuffer = SendspinAudioBuffer()
    val dataSourceFactory = SendspinDataSource.Factory(audioBuffer, clockSync)

    // State
    private val _state = MutableStateFlow(SendspinPlayerState())
    val state: StateFlow<SendspinPlayerState> = _state.asStateFlow()

    // Server and mDNS
    private var server: ApplicationEngine? = null
    private var nsdManager: NsdManager? = null
    private var registrationListener: NsdManager.RegistrationListener? = null
    private var multicastLock: WifiManager.MulticastLock? = null

    // Current handler
    private var currentHandler: SendspinWebSocketHandler? = null
    private val serviceScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    /**
     * Start the Sendspin service.
     */
    fun start() {
        AppLog.i(TAG, "Starting Sendspin service on port ${SendspinProtocol.DEFAULT_PORT}")

        // Acquire multicast lock for mDNS
        acquireMulticastLock()

        // Start WebSocket server
        startServer()

        // Register mDNS service
        registerMdns()
    }

    /**
     * Stop the Sendspin service.
     */
    fun stop() {
        AppLog.i(TAG, "Stopping Sendspin service")

        // Unregister mDNS
        unregisterMdns()

        // Stop server
        stopServer()

        // Release multicast lock
        releaseMulticastLock()

        // Cancel any ongoing operations
        serviceScope.cancel()

        // Update state
        _state.value = SendspinPlayerState()
    }

    /**
     * Update device name.
     */
    fun setDeviceName(name: String) {
        deviceName = name
        // Re-register mDNS with new name
        unregisterMdns()
        registerMdns()
    }

    private fun acquireMulticastLock() {
        try {
            val wifiManager = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            multicastLock = wifiManager.createMulticastLock("SendspinMdns").apply {
                setReferenceCounted(false)
                acquire()
            }
            AppLog.d(TAG, "Multicast lock acquired")
        } catch (e: Exception) {
            AppLog.e(TAG, "Failed to acquire multicast lock: ${e.message}")
        }
    }

    private fun releaseMulticastLock() {
        try {
            multicastLock?.release()
            multicastLock = null
            AppLog.d(TAG, "Multicast lock released")
        } catch (e: Exception) {
            AppLog.e(TAG, "Failed to release multicast lock: ${e.message}")
        }
    }

    private fun startServer() {
        server = embeddedServer(Netty, port = SendspinProtocol.DEFAULT_PORT) {
            install(WebSockets) {
                pingPeriod = Duration.ofSeconds(15)
                timeout = Duration.ofSeconds(30)
                maxFrameSize = Long.MAX_VALUE
                masking = false
            }

            routing {
                webSocket("/sendspin") {
                    handleWebSocketConnection(this)
                }
                // Also accept root path for compatibility
                webSocket("/") {
                    handleWebSocketConnection(this)
                }
            }
        }

        serviceScope.launch {
            try {
                server?.start(wait = false)
                AppLog.i(TAG, "WebSocket server started on port ${SendspinProtocol.DEFAULT_PORT}")
            } catch (e: Exception) {
                AppLog.e(TAG, "Failed to start WebSocket server: ${e.message}")
            }
        }
    }

    private fun stopServer() {
        try {
            serviceScope.launch {
                currentHandler?.close()
                currentHandler = null
            }
            server?.stop(1000, 2000)
            server = null
            AppLog.d(TAG, "WebSocket server stopped")
        } catch (e: Exception) {
            AppLog.e(TAG, "Error stopping server: ${e.message}")
        }
    }

    private suspend fun handleWebSocketConnection(session: WebSocketSession) {
        AppLog.i(TAG, "New WebSocket connection from Sendspin server")

        // Close existing handler if any
        currentHandler?.close()

        // Create new handler
        val handler = SendspinWebSocketHandler(
            clientId = clientId,
            deviceName = deviceName,
            audioBuffer = audioBuffer,
            clockSync = clockSync,
            dataSourceFactory = dataSourceFactory,
            onStreamStart = { config ->
                _state.value = _state.value.copy(streamActive = true)
                onStreamStart(config)
            },
            onStreamEnd = {
                _state.value = _state.value.copy(streamActive = false)
                onStreamEnd()
            },
            onVolumeChange = { volume ->
                _state.value = _state.value.copy(volume = volume)
                onVolumeChange(volume)
            },
            onMuteChange = { muted ->
                _state.value = _state.value.copy(muted = muted)
                onMuteChange(muted)
            }
        )
        currentHandler = handler

        // Collect handler state to service state
        serviceScope.launch {
            handler.state.collect { handlerState ->
                _state.value = handlerState
            }
        }

        // Handle the session
        handler.handleSession(session)

        // Cleanup after session ends
        if (currentHandler == handler) {
            currentHandler = null
        }
        AppLog.i(TAG, "WebSocket connection closed")
    }

    private fun registerMdns() {
        try {
            nsdManager = context.getSystemService(Context.NSD_SERVICE) as NsdManager

            val serviceInfo = NsdServiceInfo().apply {
                serviceName = deviceName
                serviceType = SendspinProtocol.SERVICE_TYPE
                port = SendspinProtocol.DEFAULT_PORT
                // TXT record required for Music Assistant discovery
                setAttribute("path", "/sendspin")
            }

            registrationListener = object : NsdManager.RegistrationListener {
                override fun onServiceRegistered(serviceInfo: NsdServiceInfo) {
                    AppLog.i(TAG, "mDNS service registered: ${serviceInfo.serviceName}")
                }

                override fun onRegistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                    AppLog.e(TAG, "mDNS registration failed: error $errorCode")
                }

                override fun onServiceUnregistered(serviceInfo: NsdServiceInfo) {
                    AppLog.d(TAG, "mDNS service unregistered")
                }

                override fun onUnregistrationFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                    AppLog.e(TAG, "mDNS unregistration failed: error $errorCode")
                }
            }

            nsdManager?.registerService(serviceInfo, NsdManager.PROTOCOL_DNS_SD, registrationListener)
        } catch (e: Exception) {
            AppLog.e(TAG, "Failed to register mDNS: ${e.message}")
        }
    }

    private fun unregisterMdns() {
        try {
            registrationListener?.let { listener ->
                nsdManager?.unregisterService(listener)
            }
            registrationListener = null
            nsdManager = null
        } catch (e: Exception) {
            AppLog.e(TAG, "Error unregistering mDNS: ${e.message}")
        }
    }

    /**
     * Get current connection state.
     */
    fun getConnectionState(): SendspinConnectionState = _state.value.connectionState

    /**
     * Check if connected to a Sendspin server.
     */
    fun isConnected(): Boolean = _state.value.connectionState == SendspinConnectionState.CONNECTED ||
            _state.value.connectionState == SendspinConnectionState.STREAMING

    /**
     * Check if currently streaming audio.
     */
    fun isStreaming(): Boolean = _state.value.streamActive

    /**
     * Get the clock sync instance for external use.
     */
    fun getClockSync(): SendspinClockSync = clockSync

    /**
     * Set volume from external control.
     */
    fun setVolume(volume: Int) {
        currentHandler?.setVolume(volume)
    }

    /**
     * Set mute state from external control.
     */
    fun setMuted(muted: Boolean) {
        currentHandler?.setMuted(muted)
    }

    /**
     * Get or create a persistent client ID.
     */
    private fun getOrCreateClientId(): String {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        var id = prefs.getString(KEY_CLIENT_ID, null)
        if (id == null) {
            id = UUID.randomUUID().toString()
            prefs.edit().putString(KEY_CLIENT_ID, id).apply()
            AppLog.i(TAG, "Generated new Sendspin client ID: $id")
        } else {
            AppLog.d(TAG, "Using existing Sendspin client ID: $id")
        }
        return id
    }
}
