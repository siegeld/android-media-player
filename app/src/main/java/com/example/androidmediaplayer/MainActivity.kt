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
import android.view.View
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.example.androidmediaplayer.databinding.ActivityMainBinding
import com.example.androidmediaplayer.model.PlayerState
import com.example.androidmediaplayer.server.MediaHttpServer
import com.example.androidmediaplayer.service.MediaPlayerService
import com.example.androidmediaplayer.util.AppLog
import com.example.androidmediaplayer.util.UpdateManager
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch
import java.net.Inet4Address
import java.net.NetworkInterface

class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "MainActivity"
        private const val DEFAULT_UPDATE_SERVER_PORT = 9742
    }

    private lateinit var binding: ActivityMainBinding
    private lateinit var updateManager: UpdateManager
    private var mediaService: MediaPlayerService? = null
    private var serviceBound = false
    private var serviceRunning = false

    private val prefs by lazy {
        getSharedPreferences("media_player_prefs", Context.MODE_PRIVATE)
    }

    private val installPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) {
        // Check if we now have permission and retry the update
        if (updateManager.canInstallPackages()) {
            AppLog.i(TAG, "Install permission granted, retrying update")
            lifecycleScope.launch {
                updateManager.downloadAndInstall()
            }
        } else {
            AppLog.w(TAG, "Install permission still not granted")
        }
    }

    private val serviceConnection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            AppLog.d(TAG, "Service connected")
            val localBinder = binder as MediaPlayerService.LocalBinder
            mediaService = localBinder.getService()
            serviceBound = true
            observePlayerState()
            setupUpdateHandler()
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            AppLog.d(TAG, "Service disconnected")
            mediaService = null
            serviceBound = false
        }
    }

    private fun setupUpdateHandler() {
        mediaService?.setUpdateHandler(object : MediaHttpServer.UpdateHandler {
            override suspend fun checkForUpdate(): MediaHttpServer.UpdateInfo? {
                val info = updateManager.checkForUpdate()
                return if (info != null) {
                    MediaHttpServer.UpdateInfo(
                        versionName = info.versionName,
                        versionCode = info.versionCode,
                        isNewer = info.isNewer,
                        currentVersion = updateManager.getCurrentVersionName(),
                        currentCode = updateManager.getCurrentVersionCode()
                    )
                } else null
            }

            override suspend fun downloadAndInstall(): Boolean {
                return try {
                    updateManager.downloadAndInstall()
                    true
                } catch (e: Exception) {
                    AppLog.e(TAG, "Update failed: ${e.message}", e)
                    false
                }
            }

            override fun getUpdateState(): String {
                return updateManager.state.value.toString()
            }
        })
        AppLog.d(TAG, "Update handler configured for HTTP API")
    }

    private val notificationPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { isGranted ->
        AppLog.d(TAG, "Notification permission result: granted=$isGranted")
        if (isGranted) {
            startMediaService()
        } else {
            AppLog.w(TAG, "Notification permission denied, service may not work properly")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AppLog.d(TAG, "onCreate")
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        updateManager = UpdateManager(this)

        loadSettings()
        setupUI()
        setupUpdate()
        updateIPAddress()
        displayVersion()
        startServiceWatchdog()

        // Handle auto-start from deploy script or intent
        handleAutoStart(intent)
    }

    private fun displayVersion() {
        val versionName = updateManager.getCurrentVersionName()
        val versionCode = updateManager.getCurrentVersionCode()
        binding.versionText.text = "v$versionName ($versionCode)"
    }

    private var watchdogJob: kotlinx.coroutines.Job? = null

    private fun startServiceWatchdog() {
        watchdogJob?.cancel()
        watchdogJob = lifecycleScope.launch {
            while (true) {
                kotlinx.coroutines.delay(30000) // Check every 30 seconds

                if (serviceRunning && !serviceBound) {
                    AppLog.w(TAG, "Watchdog: Service should be running but not bound, attempting reconnect")
                    try {
                        val intent = Intent(this@MainActivity, MediaPlayerService::class.java)
                        bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)
                    } catch (e: Exception) {
                        AppLog.e(TAG, "Watchdog: Failed to rebind service", e)
                    }
                }

                // Check if service process is alive when it should be running
                if (serviceRunning) {
                    val isServiceRunning = isServiceActuallyRunning()
                    if (!isServiceRunning) {
                        AppLog.w(TAG, "Watchdog: Service died, restarting...")
                        try {
                            startMediaService()
                        } catch (e: Exception) {
                            AppLog.e(TAG, "Watchdog: Failed to restart service", e)
                        }
                    }
                }
            }
        }
    }

    private fun isServiceActuallyRunning(): Boolean {
        val manager = getSystemService(Context.ACTIVITY_SERVICE) as android.app.ActivityManager
        @Suppress("DEPRECATION")
        for (service in manager.getRunningServices(Integer.MAX_VALUE)) {
            if (MediaPlayerService::class.java.name == service.service.className) {
                return true
            }
        }
        return false
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        intent?.let { handleAutoStart(it) }
    }

    private fun handleAutoStart(intent: Intent) {
        val autoStart = intent.getStringExtra("auto_start")
        if (autoStart == "true" && !serviceRunning) {
            AppLog.i(TAG, "Auto-starting service from intent")
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
        AppLog.d(TAG, "Loaded settings: deviceName=$deviceName, port=$port, serviceRunning=$serviceRunning")

        binding.deviceNameInput.setText(deviceName)
        binding.portInput.setText(port.toString())
    }

    private fun saveSettings() {
        val deviceName = binding.deviceNameInput.text.toString()
        val port = binding.portInput.text.toString().toIntOrNull() ?: 8765
        AppLog.d(TAG, "Saving settings: deviceName=$deviceName, port=$port, serviceRunning=$serviceRunning")

        prefs.edit()
            .putString("device_name", deviceName)
            .putInt("port", port)
            .putBoolean("service_running", serviceRunning)
            .apply()
    }

    private fun setupUI() {
        updateServiceButton()
        setupLogging()

        binding.toggleServiceButton.setOnClickListener {
            if (serviceRunning) {
                AppLog.i(TAG, "User requested to stop service")
                stopMediaService()
            } else {
                AppLog.i(TAG, "User requested to start service")
                checkPermissionsAndStart()
            }
        }
    }

    private fun setupLogging() {
        // Load logging preference
        val loggingEnabled = prefs.getBoolean("logging_enabled", false)
        AppLog.setEnabled(loggingEnabled)
        binding.logToggle.isChecked = loggingEnabled
        updateLogDisplay()

        binding.logToggle.setOnCheckedChangeListener { _, isChecked ->
            AppLog.setEnabled(isChecked)
            prefs.edit().putBoolean("logging_enabled", isChecked).apply()
            updateLogDisplay()
        }

        binding.clearLogsButton.setOnClickListener {
            AppLog.clear()
        }

        // Observe log changes
        lifecycleScope.launch {
            AppLog.logs.collectLatest { logs ->
                updateLogDisplay()
                // Auto-scroll to bottom
                binding.logScrollView.post {
                    binding.logScrollView.fullScroll(android.view.View.FOCUS_DOWN)
                }
            }
        }
    }

    private fun updateLogDisplay() {
        if (!AppLog.isEnabled()) {
            binding.logText.text = getString(R.string.logging_disabled)
            return
        }

        val logs = AppLog.logs.value
        if (logs.isEmpty()) {
            binding.logText.text = "No logs yet..."
        } else {
            binding.logText.text = logs.joinToString("\n") { it.toString() }
        }
    }

    private fun setupUpdate() {
        // Configure update server - use the gateway IP (host machine from Android's perspective)
        // The update server runs on port 8888 on the host
        val updateServerHost = prefs.getString("update_server_host", null)
        if (updateServerHost != null) {
            updateManager.setUpdateServer(updateServerHost, DEFAULT_UPDATE_SERVER_PORT)
        }

        // Show current version
        val versionInfo = "v${updateManager.getCurrentVersionName()} (${updateManager.getCurrentVersionCode()})"
        AppLog.i(TAG, "Current app version: $versionInfo")

        binding.updateButton.setOnClickListener {
            handleUpdateClick()
        }

        // Long press to configure update server
        binding.updateButton.setOnLongClickListener {
            showUpdateServerDialog()
            true
        }

        // Observe update state
        lifecycleScope.launch {
            updateManager.state.collectLatest { state ->
                updateUpdateUI(state)
            }
        }
    }

    private fun handleUpdateClick() {
        val currentState = updateManager.state.value

        when (currentState) {
            is UpdateManager.UpdateState.Idle,
            is UpdateManager.UpdateState.NoUpdate,
            is UpdateManager.UpdateState.Error -> {
                // Check for updates
                lifecycleScope.launch {
                    val info = updateManager.checkForUpdate()
                    if (info?.isNewer == true) {
                        // If update is available, start download
                        startUpdateDownload()
                    }
                }
            }
            is UpdateManager.UpdateState.UpdateAvailable -> {
                // Start download
                startUpdateDownload()
            }
            else -> {
                // Already checking/downloading/installing
                AppLog.d(TAG, "Update already in progress: $currentState")
            }
        }
    }

    private fun startUpdateDownload() {
        // Check if we have permission to install packages
        if (!updateManager.canInstallPackages()) {
            AppLog.i(TAG, "Requesting install permission")
            installPermissionLauncher.launch(updateManager.getInstallPermissionIntent())
            return
        }

        lifecycleScope.launch {
            updateManager.downloadAndInstall()
        }
    }

    private fun showUpdateServerDialog() {
        val currentHost = prefs.getString("update_server_host", "")

        val builder = android.app.AlertDialog.Builder(this)
        builder.setTitle(R.string.update_server_host)

        val input = android.widget.EditText(this)
        input.hint = "192.168.1.100"
        input.setText(currentHost)
        input.inputType = android.text.InputType.TYPE_CLASS_TEXT
        builder.setView(input)

        builder.setPositiveButton("Save") { _, _ ->
            val host = input.text.toString().trim()
            if (host.isNotEmpty()) {
                prefs.edit().putString("update_server_host", host).apply()
                updateManager.setUpdateServer(host, DEFAULT_UPDATE_SERVER_PORT)
                AppLog.i(TAG, "Update server set to: $host:$DEFAULT_UPDATE_SERVER_PORT")

                // Also configure remote logging
                val deviceName = binding.deviceNameInput.text.toString().ifBlank { "Android Media Player" }
                (application as MediaPlayerApp).configureRemoteLogging(host, deviceName)
            }
        }

        builder.setNegativeButton("Cancel") { dialog, _ ->
            dialog.cancel()
        }

        builder.show()
    }

    private fun updateUpdateUI(state: UpdateManager.UpdateState) {
        binding.updateStatusText.visibility = View.VISIBLE

        when (state) {
            is UpdateManager.UpdateState.Idle -> {
                binding.updateButton.text = getString(R.string.check_update)
                binding.updateButton.isEnabled = true
                binding.updateStatusText.visibility = View.GONE
            }
            is UpdateManager.UpdateState.Checking -> {
                binding.updateButton.text = getString(R.string.checking_update)
                binding.updateButton.isEnabled = false
                binding.updateStatusText.text = "Checking for updates..."
            }
            is UpdateManager.UpdateState.NoUpdate -> {
                binding.updateButton.text = getString(R.string.check_update)
                binding.updateButton.isEnabled = true
                binding.updateStatusText.text = getString(R.string.no_update)
            }
            is UpdateManager.UpdateState.UpdateAvailable -> {
                binding.updateButton.text = "Install ${state.info.versionName}"
                binding.updateButton.isEnabled = true
                binding.updateStatusText.text = getString(R.string.update_available, state.info.versionName)
            }
            is UpdateManager.UpdateState.Downloading -> {
                binding.updateButton.text = getString(R.string.downloading_update, state.progress)
                binding.updateButton.isEnabled = false
                binding.updateStatusText.text = "Downloading: ${state.progress}%"
            }
            is UpdateManager.UpdateState.Installing -> {
                binding.updateButton.text = getString(R.string.installing_update)
                binding.updateButton.isEnabled = false
                binding.updateStatusText.text = "Installing update..."
            }
            is UpdateManager.UpdateState.Error -> {
                binding.updateButton.text = getString(R.string.check_update)
                binding.updateButton.isEnabled = true
                binding.updateStatusText.text = getString(R.string.update_error, state.message)
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
                    AppLog.d(TAG, "Notification permission already granted")
                    startMediaService()
                }
                else -> {
                    AppLog.d(TAG, "Requesting notification permission")
                    notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                }
            }
        } else {
            AppLog.d(TAG, "Android < 13, no notification permission needed")
            startMediaService()
        }
    }

    private fun startMediaService() {
        val deviceName = binding.deviceNameInput.text.toString().ifBlank { "Android Media Player" }
        val port = binding.portInput.text.toString().toIntOrNull() ?: 8765

        AppLog.i(TAG, "Starting media service: deviceName=$deviceName, port=$port")

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
            AppLog.i(TAG, "Media service started successfully")
        } catch (e: Exception) {
            AppLog.e(TAG, "Failed to start media service: ${e.message}", e)
        }
    }

    private fun stopMediaService() {
        AppLog.i(TAG, "Stopping media service")

        if (serviceBound) {
            AppLog.d(TAG, "Unbinding from service")
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
        AppLog.i(TAG, "Media service stopped")
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
            AppLog.i(TAG, "Service accessible at: $url")
            binding.ipAddressText.text = url
        } else if (ip != null) {
            AppLog.d(TAG, "IP address: $ip (service not running)")
            binding.ipAddressText.text = "IP: $ip (service not running)"
        } else {
            AppLog.w(TAG, "No network connection detected")
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
                AppLog.v(TAG, "Got IP from WiFi: $ip")
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
                        AppLog.v(TAG, "Got IP from network interface ${networkInterface.name}: ${address.hostAddress}")
                        return address.hostAddress
                    }
                }
            }
        } catch (e: Exception) {
            AppLog.e(TAG, "Error getting IP address: ${e.message}", e)
        }
        return null
    }

    private fun observePlayerState() {
        AppLog.d(TAG, "Starting to observe player state")
        lifecycleScope.launch {
            mediaService?.playerState?.collectLatest { state ->
                AppLog.v(TAG, "Player state update: state=${state.state}, title=${state.mediaTitle}")
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

        // Show now playing info (title - artist)
        if (state.mediaTitle != null || state.mediaArtist != null) {
            binding.nowPlayingText.visibility = android.view.View.VISIBLE
            val nowPlaying = buildString {
                state.mediaTitle?.let { append(it) }
                state.mediaArtist?.let {
                    if (isNotEmpty()) append(" - ")
                    append(it)
                }
            }
            binding.nowPlayingText.text = nowPlaying.ifEmpty { "Unknown" }
        } else {
            binding.nowPlayingText.visibility = android.view.View.GONE
        }

        // Show URL
        if (!state.mediaUrl.isNullOrEmpty()) {
            binding.nowPlayingUrlText.visibility = android.view.View.VISIBLE
            binding.nowPlayingUrlText.text = state.mediaUrl
        } else {
            binding.nowPlayingUrlText.visibility = android.view.View.GONE
        }
    }

    override fun onStart() {
        super.onStart()
        AppLog.d(TAG, "onStart, serviceRunning=$serviceRunning")
        if (serviceRunning) {
            // Must call startForegroundService before bindService to ensure
            // onStartCommand is called (which starts the HTTP server)
            val deviceName = binding.deviceNameInput.text.toString().ifBlank { "Android Media Player" }
            val port = binding.portInput.text.toString().toIntOrNull() ?: 8765
            val intent = Intent(this, MediaPlayerService::class.java).apply {
                putExtra(MediaPlayerService.EXTRA_PORT, port)
                putExtra(MediaPlayerService.EXTRA_DEVICE_NAME, deviceName)
            }
            ContextCompat.startForegroundService(this, intent)
            bindService(intent, serviceConnection, Context.BIND_AUTO_CREATE)
        }
    }

    override fun onStop() {
        super.onStop()
        AppLog.d(TAG, "onStop, serviceBound=$serviceBound")
        // Don't unbind here - the foreground service should continue running
        // when the Activity is in the background. Only unbind in onDestroy.
    }

    override fun onDestroy() {
        AppLog.d(TAG, "onDestroy, serviceBound=$serviceBound")
        watchdogJob?.cancel()
        if (serviceBound) {
            unbindService(serviceConnection)
            serviceBound = false
        }
        super.onDestroy()
    }
}
