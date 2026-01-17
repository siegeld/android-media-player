package com.example.androidmediaplayer.sendspin

import android.util.Base64
import com.example.androidmediaplayer.util.AppLog
import io.ktor.websocket.*
import kotlinx.coroutines.*
import kotlinx.coroutines.channels.ClosedReceiveChannelException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Handles Sendspin WebSocket protocol for a single connection.
 *
 * Protocol flow:
 * 1. Server connects to client's WebSocket endpoint
 * 2. Client sends client/hello
 * 3. Server responds with server/hello
 * 4. Client sends client/time for clock sync (multiple times)
 * 5. Server responds with server/time
 * 6. Server sends stream/start when ready to play
 * 7. Server sends binary audio frames
 * 8. Client sends client/state updates
 * 9. Server sends server/command for volume/mute control
 */
class SendspinWebSocketHandler(
    private val clientId: String,
    private val deviceName: String,
    private val audioBuffer: SendspinAudioBuffer,
    private val clockSync: SendspinClockSync,
    private val dataSourceFactory: SendspinDataSource.Factory,
    private val onStreamStart: (SendspinStreamConfig) -> Unit,
    private val onStreamEnd: () -> Unit,
    private val onVolumeChange: (Int) -> Unit,
    private val onMuteChange: (Boolean) -> Unit
) {
    companion object {
        private const val TAG = "SendspinWSHandler"
        private const val CLOCK_SYNC_INTERVAL_MS = 30_000L // Re-sync every 30 seconds
        private const val INITIAL_SYNC_COUNT = 5 // Number of initial clock syncs
        private const val AUDIO_LOG_INTERVAL = 100 // Log every Nth audio chunk
    }

    private var audioChunkCount = 0

    private val _state = MutableStateFlow(SendspinPlayerState())
    val state: StateFlow<SendspinPlayerState> = _state.asStateFlow()

    private var session: WebSocketSession? = null
    private var clockSyncJob: Job? = null
    private var currentStreamConfig: SendspinStreamConfig? = null

    private val deviceInfo = SendspinProtocol.DeviceInfo(
        productName = android.os.Build.MODEL,
        manufacturer = android.os.Build.MANUFACTURER,
        softwareVersion = android.os.Build.VERSION.RELEASE
    )

    /**
     * Handle a new WebSocket session.
     */
    suspend fun handleSession(webSocketSession: WebSocketSession) {
        AppLog.d(TAG, "handleSession starting")
        session = webSocketSession
        updateState { copy(connectionState = SendspinConnectionState.CONNECTING) }

        try {
            // Send client/hello immediately when connection is established
            // Server connects to us, we identify ourselves first
            AppLog.d(TAG, "Sending client/hello...")
            sendHello()
            updateState { copy(connectionState = SendspinConnectionState.HANDSHAKING) }

            // Process incoming messages - wait for server/hello response
            AppLog.d(TAG, "Waiting for server response...")
            for (frame in webSocketSession.incoming) {
                when (frame) {
                    is Frame.Text -> {
                        AppLog.d(TAG, "Received TEXT frame")
                        handleTextMessage(frame.readText())
                    }
                    is Frame.Binary -> handleBinaryMessage(frame.readBytes())
                    is Frame.Close -> {
                        AppLog.d(TAG, "Received close frame")
                        break
                    }
                    else -> { /* Ignore ping/pong */ }
                }
            }
            AppLog.d(TAG, "Frame loop exited normally")
        } catch (e: ClosedReceiveChannelException) {
            AppLog.d(TAG, "WebSocket channel closed: ${e.message}")
        } catch (e: Exception) {
            AppLog.e(TAG, "WebSocket error: ${e.javaClass.simpleName}: ${e.message}")
            e.printStackTrace()
            updateState { copy(connectionState = SendspinConnectionState.ERROR, errorMessage = e.message) }
        } finally {
            AppLog.d(TAG, "Cleaning up session")
            cleanup()
        }
    }

    private suspend fun sendHello() {
        val hello = SendspinProtocol.createClientHello(
            clientId = clientId,
            name = deviceName,
            deviceInfo = deviceInfo,
            bufferCapacity = audioBuffer.capacity()
        )
        session?.send(Frame.Text(hello))
        AppLog.d(TAG, "Sent client/hello")
    }

    private suspend fun handleTextMessage(text: String) {
        val message = SendspinProtocol.parseMessage(text)
        if (message == null) {
            AppLog.w(TAG, "Failed to parse message: $text")
            return
        }

        AppLog.d(TAG, "Received message: ${message.type}")

        when (message.type) {
            SendspinProtocol.MessageType.SERVER_HELLO -> handleServerHello(message.payload)
            SendspinProtocol.MessageType.SERVER_TIME -> handleServerTime(message.payload)
            SendspinProtocol.MessageType.STREAM_START -> handleStreamStart(message.payload)
            SendspinProtocol.MessageType.STREAM_CLEAR -> handleStreamClear(message.payload)
            SendspinProtocol.MessageType.STREAM_END -> handleStreamEnd(message.payload)
            SendspinProtocol.MessageType.SERVER_COMMAND -> handleServerCommand(message.payload)
            SendspinProtocol.MessageType.SERVER_STATE -> handleServerState(message.payload)
            else -> AppLog.w(TAG, "Unknown message type: ${message.type}")
        }
    }

    private suspend fun handleServerHello(payload: com.google.gson.JsonObject) {
        val serverHello = SendspinProtocol.parseServerHello(payload)
        if (serverHello == null) {
            AppLog.e(TAG, "Failed to parse server/hello")
            return
        }

        AppLog.i(TAG, "Received server/hello from: ${serverHello.name} (${serverHello.serverId})")

        // We already sent client/hello when connection started, proceed to clock sync
        updateState {
            copy(
                connectionState = SendspinConnectionState.SYNCING_CLOCK,
                serverId = serverHello.serverId,
                serverName = serverHello.name
            )
        }

        // Start clock synchronization
        startClockSync()
    }

    private fun startClockSync() {
        clockSyncJob?.cancel()
        clockSyncJob = CoroutineScope(Dispatchers.IO).launch {
            // Initial sync: send multiple time requests
            repeat(INITIAL_SYNC_COUNT) {
                sendTimeRequest()
                delay(100)
            }

            // Periodic sync
            while (isActive) {
                delay(CLOCK_SYNC_INTERVAL_MS)
                sendTimeRequest()
            }
        }
    }

    private suspend fun sendTimeRequest() {
        val clientTime = clockSync.localTimeMicros()
        val timeMessage = SendspinProtocol.createClientTime(clientTime)
        session?.send(Frame.Text(timeMessage))
    }

    private fun handleServerTime(payload: com.google.gson.JsonObject) {
        val serverTime = SendspinProtocol.parseServerTime(payload)
        if (serverTime == null) {
            AppLog.e(TAG, "Failed to parse server/time")
            return
        }

        val offset = clockSync.onTimeResponse(
            clientTransmitted = serverTime.clientTransmitted,
            serverReceived = serverTime.serverReceived,
            serverTransmitted = serverTime.serverTransmitted
        )

        val synced = clockSync.isSynced()
        AppLog.d(TAG, "Clock sync: offset=${offset}us, synced=$synced, samples=${clockSync.getSampleCount()}")

        updateState {
            copy(
                clockOffsetMicros = offset,
                clockSynced = synced,
                connectionState = if (synced && connectionState == SendspinConnectionState.SYNCING_CLOCK) {
                    SendspinConnectionState.CONNECTED
                } else connectionState
            )
        }

        // Send initial state once connected
        if (synced && _state.value.connectionState == SendspinConnectionState.CONNECTED) {
            CoroutineScope(Dispatchers.IO).launch {
                sendPlayerState()
            }
        }
    }

    private suspend fun handleStreamStart(payload: com.google.gson.JsonObject) {
        AppLog.i(TAG, "=== STREAM/START RECEIVED ===")
        AppLog.d(TAG, "Raw payload: $payload")

        val streamStart = SendspinProtocol.parseStreamStart(payload)
        val playerConfig = streamStart?.player
        if (playerConfig == null) {
            AppLog.w(TAG, "stream/start without player config")
            return
        }

        AppLog.i(TAG, "Stream starting: codec=${playerConfig.codec}, sampleRate=${playerConfig.sampleRate}Hz, channels=${playerConfig.channels}, bitDepth=${playerConfig.bitDepth}")
        audioChunkCount = 0 // Reset chunk counter for new stream

        // Decode codec header if present
        val codecHeader = playerConfig.codecHeader?.let {
            try {
                Base64.decode(it, Base64.DEFAULT)
            } catch (e: Exception) {
                null
            }
        }

        val config = SendspinStreamConfig(
            codec = playerConfig.codec,
            sampleRate = playerConfig.sampleRate,
            channels = playerConfig.channels,
            bitDepth = playerConfig.bitDepth,
            codecHeader = codecHeader
        )
        currentStreamConfig = config

        // Clear buffer for new stream
        audioBuffer.clear()

        updateState {
            copy(
                connectionState = SendspinConnectionState.STREAMING,
                streamActive = true,
                currentCodec = config.codec,
                sampleRate = config.sampleRate,
                channels = config.channels
            )
        }

        AppLog.i(TAG, "Calling onStreamStart callback with config: $config")
        onStreamStart(config)
        AppLog.i(TAG, "onStreamStart callback completed")
    }

    private fun handleStreamClear(payload: com.google.gson.JsonObject) {
        val streamClear = SendspinProtocol.parseStreamClear(payload)
        if (streamClear?.roles?.contains("player") == true) {
            AppLog.d(TAG, "Stream clear received")
            audioBuffer.clear()
        }
    }

    private fun handleStreamEnd(payload: com.google.gson.JsonObject) {
        AppLog.i(TAG, "Stream ended")
        currentStreamConfig = null
        audioBuffer.clear()

        updateState {
            copy(
                connectionState = SendspinConnectionState.CONNECTED,
                streamActive = false,
                currentCodec = null,
                sampleRate = null,
                channels = null
            )
        }

        onStreamEnd()
    }

    private fun handleServerCommand(payload: com.google.gson.JsonObject) {
        val command = SendspinProtocol.parseServerCommand(payload)
        val playerCommand = command?.player ?: return

        AppLog.d(TAG, "Server command: ${playerCommand.command}")

        when (playerCommand.command) {
            "volume" -> {
                playerCommand.volume?.let { volume ->
                    updateState { copy(volume = volume) }
                    onVolumeChange(volume)
                }
            }
            "mute" -> {
                playerCommand.mute?.let { muted ->
                    updateState { copy(muted = muted) }
                    onMuteChange(muted)
                }
            }
        }

        // Send state update after processing command
        CoroutineScope(Dispatchers.IO).launch {
            sendPlayerState()
        }
    }

    private fun handleServerState(payload: com.google.gson.JsonObject) {
        // Server state updates (for controller role, not implemented)
        AppLog.d(TAG, "Received server/state (ignored - player only)")
    }

    private fun handleBinaryMessage(data: ByteArray) {
        if (data.isEmpty()) return

        val messageType = data[0]

        when (messageType) {
            SendspinProtocol.BinaryType.AUDIO -> {
                val audioChunk = SendspinProtocol.parseBinaryAudio(data)
                if (audioChunk != null) {
                    audioChunkCount++
                    val written = audioBuffer.write(audioChunk.timestampMicros, audioChunk.audioData)
                    if (!written) {
                        // Only log drops periodically to avoid flooding
                        if (audioChunkCount % AUDIO_LOG_INTERVAL == 0) {
                            AppLog.w(TAG, "Audio buffer full, dropping chunks (total: $audioChunkCount)")
                        }
                    } else {
                        dataSourceFactory.notifyDataAvailable()
                        // Log every Nth chunk to reduce noise
                        if (audioChunkCount % AUDIO_LOG_INTERVAL == 1) {
                            AppLog.d(TAG, "Audio chunk #$audioChunkCount: ${audioChunk.audioData.size} bytes, buffer: ${audioBuffer.sizeBytes()}/${audioBuffer.capacity()}")
                        }
                    }

                    // Update buffer health
                    updateState { copy(bufferHealthPercent = audioBuffer.healthPercent()) }
                }
            }
            else -> {
                // Ignore other binary message types (artwork, visualizer)
                AppLog.d(TAG, "Ignoring binary message type: $messageType")
            }
        }
    }

    private suspend fun sendPlayerState() {
        val currentState = _state.value
        // Always report synchronized when operational - this means "ready", not "playing"
        // Only report error when there's an actual error condition
        val playerState = if (currentState.connectionState == SendspinConnectionState.ERROR) {
            SendspinProtocol.PlayerState.ERROR
        } else {
            SendspinProtocol.PlayerState.SYNCHRONIZED
        }

        val stateMessage = SendspinProtocol.createClientState(
            state = playerState,
            volume = currentState.volume,
            muted = currentState.muted
        )
        session?.send(Frame.Text(stateMessage))
    }

    private fun updateState(update: SendspinPlayerState.() -> SendspinPlayerState) {
        _state.value = _state.value.update()
    }

    private fun cleanup() {
        clockSyncJob?.cancel()
        clockSyncJob = null
        session = null
        currentStreamConfig = null

        updateState {
            copy(
                connectionState = SendspinConnectionState.DISCONNECTED,
                serverId = null,
                serverName = null,
                streamActive = false
            )
        }
    }

    /**
     * Update volume from external source (e.g., local control).
     */
    fun setVolume(volume: Int) {
        updateState { copy(volume = volume.coerceIn(0, 100)) }
        CoroutineScope(Dispatchers.IO).launch {
            sendPlayerState()
        }
    }

    /**
     * Update mute state from external source.
     */
    fun setMuted(muted: Boolean) {
        updateState { copy(muted = muted) }
        CoroutineScope(Dispatchers.IO).launch {
            sendPlayerState()
        }
    }

    /**
     * Close the connection.
     */
    suspend fun close() {
        session?.close(CloseReason(CloseReason.Codes.NORMAL, "Client closing"))
        cleanup()
    }
}
