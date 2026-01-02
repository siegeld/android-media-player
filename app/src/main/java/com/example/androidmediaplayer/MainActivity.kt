package com.example.androidmediaplayer

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.text.format.Formatter
import android.util.Log
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.example.androidmediaplayer.databinding.ActivityMainBinding
import com.example.androidmediaplayer.model.PlayerState
import com.example.androidmediaplayer.service.MediaPlayerService
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch
import java.net.Inet4Address
import java.net.NetworkInterface

class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "MainActivity"
    }

    private lateinit var binding: ActivityMainBinding
    private var mediaService: MediaPlayerService? = null
    private var serviceBound = false
    private var serviceRunning = false

    private val prefs by lazy {
        getSharedPreferences("media_player_prefs", Context.MODE_PRIVATE)
    }

    private val serviceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            Log.d(TAG, "Service connected")
            val localBinder = binder as MediaPlayerService.LocalBinder
            mediaService = localBinder.getService()
            serviceBound = true
            observePlayerState()
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            Log.d(TAG, "Service disconnected")
            mediaService = null
            serviceBound = false
        }
    }

    private val notificationPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { isGranted ->
        Log.d(TAG, "Notification permission result: granted=$isGranted")
        if (isGranted) {
            startMediaService()
        } else {
            Log.w(TAG, "Notification permission denied, service may not work properly")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Log.d(TAG, "onCreate")
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        loadSettings()
        setupUI()
        updateIPAddress()

        // Handle auto-start from deploy script or intent
        handleAutoStart(intent)
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        intent?.let { handleAutoStart(it) }
    }

    private fun handleAutoStart(intent: Intent) {
        val autoStart = intent.getStringExtra("auto_start")
        if (autoStart == "true" && !serviceRunning) {
            Log.i(TAG, "Auto-starting service from intent")
            // Small delay to ensure UI is ready
            binding.root.postDelayed({
                checkPermissionsAndStart()
            }, 500)
        }
    }

    private fun loadSettings() {
        val deviceName = prefs.getString("device_name", "Android Media Player")
        val port = prefs.getInt("port", 8765)
        serviceRunning = prefs.getBoolean("service_running", false)
        Log.d(TAG, "Loaded settings: deviceName=$deviceName, port=$port, serviceRunning=$serviceRunning")

        binding.deviceNameInput.setText(deviceName)
        binding.portInput.setText(port.toString())
    }

    private fun saveSettings() {
        val deviceName = binding.deviceNameInput.text.toString()
        val port = binding.portInput.text.toString().toIntOrNull() ?: 8765
        Log.d(TAG, "Saving settings: deviceName=$deviceName, port=$port, serviceRunning=$serviceRunning")

        prefs.edit()
            .putString("device_name", deviceName)
            .putInt("port", port)
            .putBoolean("service_running", serviceRunning)
            .apply()
    }

    private fun setupUI() {
        updateServiceButton()

        binding.toggleServiceButton.setOnClickListener {
            if (serviceRunning) {
                Log.i(TAG, "User requested to stop service")
                stopMediaService()
            } else {
                Log.i(TAG, "User requested to start service")
                checkPermissionsAndStart()
            }
        }
    }

    private fun checkPermissionsAndStart() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            when {
                ContextCompat.checkSelfPermission(
                    this,
                    Manifest.permission.POST_NOTIFICATIONS
                ) == PackageManager.PERMISSION_GRANTED -> {
                    Log.d(TAG, "Notification permission already granted")
                    startMediaService()
                }
                else -> {
                    Log.d(TAG, "Requesting notification permission")
                    notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                }
            }
        } else {
            Log.d(TAG, "Android < 13, no notification permission needed")
            startMediaService()
        }
    }

    private fun startMediaService() {
        val deviceName = binding.deviceNameInput.text.toString().ifBlank { "Android Media Player" }
        val port = binding.portInput.text.toString().toIntOrNull() ?: 8765

        Log.i(TAG, "Starting media service: deviceName=$deviceName, port=$port")

        val intent = Intent(this, MediaPlayerService::class.java).apply {
            putExtra(MediaPlayerService.EXTRA_PORT, port)
            putExtra(MediaPlayerService.EXTRA_DEVICE_NAME, deviceName)
        }

        try {
            ContextCompat.startForegroundService(this, intent)
            bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)
            serviceRunning = true
            saveSettings()
            updateServiceButton()
            updateIPAddress()
            Log.i(TAG, "Media service started successfully")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start media service: ${e.message}", e)
        }
    }

    private fun stopMediaService() {
        Log.i(TAG, "Stopping media service")

        if (serviceBound) {
            Log.d(TAG, "Unbinding from service")
            unbindService(serviceConnection)
            serviceBound = false
        }

        val intent = Intent(this, MediaPlayerService::class.java)
        stopService(intent)

        mediaService = null
        serviceRunning = false
        saveSettings()
        updateServiceButton()
        updatePlayerStateUI(PlayerState())
        Log.i(TAG, "Media service stopped")
    }

    private fun updateServiceButton() {
        if (serviceRunning) {
            binding.toggleServiceButton.text = getString(R.string.stop_service)
            binding.statusText.text = getString(R.string.service_running)
            binding.deviceNameInput.isEnabled = false
            binding.portInput.isEnabled = false
        } else {
            binding.toggleServiceButton.text = getString(R.string.start_service)
            binding.statusText.text = getString(R.string.service_stopped)
            binding.deviceNameInput.isEnabled = true
            binding.portInput.isEnabled = true
        }
    }

    private fun updateIPAddress() {
        val ip = getLocalIpAddress()
        val port = binding.portInput.text.toString().toIntOrNull() ?: 8765

        if (ip != null && serviceRunning) {
            val url = "http://$ip:$port"
            Log.i(TAG, "Service accessible at: $url")
            binding.ipAddressText.text = url
        } else if (ip != null) {
            Log.d(TAG, "IP address: $ip (service not running)")
            binding.ipAddressText.text = "IP: $ip (service not running)"
        } else {
            Log.w(TAG, "No network connection detected")
            binding.ipAddressText.text = "No network connection"
        }
    }

    private fun getLocalIpAddress(): String? {
        try {
            // Try WiFi first
            val wifiManager = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            val wifiInfo = wifiManager.connectionInfo
            val ipInt = wifiInfo.ipAddress
            if (ipInt != 0) {
                @Suppress("DEPRECATION")
                val ip = Formatter.formatIpAddress(ipInt)
                Log.v(TAG, "Got IP from WiFi: $ip")
                return ip
            }

            // Fallback to network interfaces
            val interfaces = NetworkInterface.getNetworkInterfaces()
            while (interfaces.hasMoreElements()) {
                val networkInterface = interfaces.nextElement()
                val addresses = networkInterface.inetAddresses
                while (addresses.hasMoreElements()) {
                    val address = addresses.nextElement()
                    if (!address.isLoopbackAddress && address is Inet4Address) {
                        Log.v(TAG, "Got IP from network interface ${networkInterface.name}: ${address.hostAddress}")
                        return address.hostAddress
                    }
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error getting IP address: ${e.message}", e)
        }
        return null
    }

    private fun observePlayerState() {
        Log.d(TAG, "Starting to observe player state")
        lifecycleScope.launch {
            mediaService?.playerState?.collectLatest { state ->
                Log.v(TAG, "Player state update: state=${state.state}, title=${state.mediaTitle}")
                updatePlayerStateUI(state)
            }
        }
    }

    private fun updatePlayerStateUI(state: PlayerState) {
        val stateText = when (state.state) {
            PlayerState.STATE_PLAYING -> getString(R.string.status_playing)
            PlayerState.STATE_PAUSED -> getString(R.string.status_paused)
            PlayerState.STATE_BUFFERING -> "Buffering..."
            else -> getString(R.string.status_idle)
        }

        binding.playerStateText.text = "Player: $stateText | Volume: ${(state.volume * 100).toInt()}%"

        if (state.mediaTitle != null) {
            binding.nowPlayingText.visibility = android.view.View.VISIBLE
            val nowPlaying = buildString {
                append(state.mediaTitle)
                state.mediaArtist?.let { append(" - $it") }
            }
            binding.nowPlayingText.text = nowPlaying
        } else {
            binding.nowPlayingText.visibility = android.view.View.GONE
        }
    }

    override fun onStart() {
        super.onStart()
        Log.d(TAG, "onStart, serviceRunning=$serviceRunning")
        if (serviceRunning) {
            val intent = Intent(this, MediaPlayerService::class.java)
            bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)
        }
    }

    override fun onStop() {
        super.onStop()
        Log.d(TAG, "onStop, serviceBound=$serviceBound")
        if (serviceBound) {
            unbindService(serviceConnection)
            serviceBound = false
        }
    }
}
