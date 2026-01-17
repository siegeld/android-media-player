package com.example.androidmediaplayer.service

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.wifi.WifiManager
import android.os.Binder
import android.os.IBinder
import android.os.PowerManager
import android.support.v4.media.session.MediaSessionCompat
import androidx.core.app.NotificationCompat
import com.example.androidmediaplayer.util.AppLog
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.Metadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.extractor.metadata.icy.IcyInfo
import com.example.androidmediaplayer.MainActivity
import com.example.androidmediaplayer.MediaPlayerApp
import com.example.androidmediaplayer.R
import com.example.androidmediaplayer.model.PlayerState
import com.example.androidmediaplayer.server.MediaHttpServer
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

class MediaPlayerService : Service() {

    companion object {
        private const val TAG = "MediaPlayerService"
        const val NOTIFICATION_ID = 1
        const val ACTION_PLAY = "com.example.androidmediaplayer.PLAY"
        const val ACTION_PAUSE = "com.example.androidmediaplayer.PAUSE"
        const val ACTION_STOP = "com.example.androidmediaplayer.STOP"
        const val EXTRA_PORT = "extra_port"
        const val EXTRA_DEVICE_NAME = "extra_device_name"
    }

    private val binder = LocalBinder()
    private var player: ExoPlayer? = null
    private var mediaSession: MediaSessionCompat? = null
    private var httpServer: MediaHttpServer? = null
    private val serviceScope = CoroutineScope(Dispatchers.Main + SupervisorJob())
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    private val _playerState = MutableStateFlow(PlayerState())
    val playerState: StateFlow<PlayerState> = _playerState.asStateFlow()

    private var currentTitle: String? = null
    private var currentArtist: String? = null
    private var currentUrl: String? = null
    private var deviceName: String = "Android Media Player"

    inner class LocalBinder : Binder() {
        fun getService(): MediaPlayerService = this@MediaPlayerService
    }

    override fun onBind(intent: Intent?): IBinder {
        AppLog.d(TAG, "Service bound")
        return binder
    }

    override fun onCreate() {
        super.onCreate()
        AppLog.i(TAG, "Service created")
        initializePlayer()
        initializeMediaSession()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        AppLog.d(TAG, "onStartCommand: action=${intent?.action}")
        when (intent?.action) {
            ACTION_PLAY -> {
                AppLog.d(TAG, "Received ACTION_PLAY from notification")
                play()
            }
            ACTION_PAUSE -> {
                AppLog.d(TAG, "Received ACTION_PAUSE from notification")
                pause()
            }
            ACTION_STOP -> {
                AppLog.d(TAG, "Received ACTION_STOP from notification")
                stop()
            }
            else -> {
                val port = intent?.getIntExtra(EXTRA_PORT, 8765) ?: 8765
                deviceName = intent?.getStringExtra(EXTRA_DEVICE_NAME) ?: "Android Media Player"
                AppLog.i(TAG, "Starting foreground service: deviceName=$deviceName, port=$port")
                startForegroundService(port)
            }
        }
        return START_STICKY
    }

    private fun startForegroundService(port: Int) {
        AppLog.d(TAG, "Starting foreground with notification")
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                createNotification(),
                android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK
            )
        } else {
            startForeground(NOTIFICATION_ID, createNotification())
        }
        acquireWakeLocks()
        startHttpServer(port)
    }

    private fun acquireWakeLocks() {
        if (wakeLock == null) {
            val powerManager = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = powerManager.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "AndroidMediaPlayer::ServiceWakeLock"
            ).apply {
                setReferenceCounted(false)
                acquire()
            }
            AppLog.i(TAG, "Partial wake lock acquired")
        }

        if (wifiLock == null) {
            val wifiManager = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            @Suppress("DEPRECATION")
            wifiLock = wifiManager.createWifiLock(
                WifiManager.WIFI_MODE_FULL_HIGH_PERF,
                "AndroidMediaPlayer::WifiLock"
            ).apply {
                setReferenceCounted(false)
                acquire()
            }
            AppLog.i(TAG, "WiFi lock acquired")
        }
    }

    private fun releaseWakeLocks() {
        wakeLock?.let {
            if (it.isHeld) {
                it.release()
                AppLog.i(TAG, "Partial wake lock released")
            }
        }
        wakeLock = null

        wifiLock?.let {
            if (it.isHeld) {
                it.release()
                AppLog.i(TAG, "WiFi lock released")
            }
        }
        wifiLock = null
    }

    private fun initializePlayer() {
        AppLog.d(TAG, "Initializing ExoPlayer")
        player = ExoPlayer.Builder(this).build().apply {
            // Keep CPU and WiFi awake during playback for streaming
            setWakeMode(C.WAKE_MODE_NETWORK)
            addListener(object : Player.Listener {
                override fun onPlaybackStateChanged(playbackState: Int) {
                    val stateStr = when (playbackState) {
                        Player.STATE_IDLE -> "IDLE"
                        Player.STATE_BUFFERING -> "BUFFERING"
                        Player.STATE_READY -> "READY"
                        Player.STATE_ENDED -> "ENDED"
                        else -> "UNKNOWN($playbackState)"
                    }
                    AppLog.d(TAG, "Playback state changed: $stateStr")
                    updatePlayerState()
                }

                override fun onIsPlayingChanged(isPlaying: Boolean) {
                    AppLog.d(TAG, "isPlaying changed: $isPlaying")
                    updatePlayerState()
                    updateNotification()
                }

                override fun onMediaMetadataChanged(mediaMetadata: MediaMetadata) {
                    AppLog.d(TAG, "Media metadata changed: title=${mediaMetadata.title}, artist=${mediaMetadata.artist}, displayTitle=${mediaMetadata.displayTitle}")
                    // Try different metadata fields
                    val newTitle = mediaMetadata.title?.toString()
                        ?: mediaMetadata.displayTitle?.toString()
                        ?: mediaMetadata.albumTitle?.toString()
                    val newArtist = mediaMetadata.artist?.toString()
                        ?: mediaMetadata.albumArtist?.toString()

                    if (newTitle != null) currentTitle = newTitle
                    if (newArtist != null) currentArtist = newArtist

                    updatePlayerState()
                    updateNotification()
                }

                override fun onMetadata(metadata: Metadata) {
                    // Handle ICY metadata from internet radio streams
                    AppLog.d(TAG, "onMetadata: ${metadata.length()} entries")
                    for (i in 0 until metadata.length()) {
                        val entry = metadata.get(i)
                        AppLog.d(TAG, "Metadata entry $i: ${entry::class.simpleName} = $entry")
                        if (entry is IcyInfo) {
                            AppLog.i(TAG, "ICY metadata: title=${entry.title}, url=${entry.url}")
                            entry.title?.let { icyTitle ->
                                // ICY title often has format "Artist - Title"
                                if (icyTitle.contains(" - ")) {
                                    val parts = icyTitle.split(" - ", limit = 2)
                                    currentArtist = parts[0].trim()
                                    currentTitle = parts[1].trim()
                                } else {
                                    currentTitle = icyTitle
                                }
                                updatePlayerState()
                                updateNotification()
                            }
                        }
                    }
                }

                override fun onPlayerError(error: PlaybackException) {
                    AppLog.e(TAG, "Player error: ${error.errorCodeName} - ${error.message}", error)
                    handlePlaybackError(error)
                }
            })
        }
        AppLog.i(TAG, "ExoPlayer initialized successfully")
    }

    private fun initializeMediaSession() {
        AppLog.d(TAG, "Initializing MediaSession")
        mediaSession = MediaSessionCompat(this, "MediaPlayerService").apply {
            isActive = true
        }
        AppLog.i(TAG, "MediaSession initialized")
    }

    private fun startHttpServer(port: Int) {
        // Don't start if already running
        if (httpServer != null) {
            AppLog.d(TAG, "HTTP server already running, skipping start")
            return
        }
        AppLog.i(TAG, "Starting HTTP server on port $port")
        serviceScope.launch(Dispatchers.IO) {
            try {
                httpServer = MediaHttpServer(this@MediaPlayerService, port, deviceName)
                httpServer?.nameChangeHandler = object : MediaHttpServer.NameChangeHandler {
                    override fun onNameChanged(newName: String) {
                        deviceName = newName
                        // Save to SharedPreferences
                        val prefs = getSharedPreferences("media_player_prefs", Context.MODE_PRIVATE)
                        prefs.edit().putString("device_name", newName).apply()
                        // Update RemoteLogger so logs use the new name
                        MediaPlayerApp.remoteLogger?.updateDeviceName(newName)
                        AppLog.i(TAG, "Device name changed and saved: $newName")
                    }
                }
                httpServer?.start()
                AppLog.i(TAG, "HTTP server started successfully on port $port")
            } catch (e: Exception) {
                AppLog.e(TAG, "Failed to start HTTP server: ${e.message}", e)
            }
        }
    }

    fun playMedia(url: String, title: String? = null, artist: String? = null) {
        AppLog.i(TAG, "playMedia: url=$url, title=$title, artist=$artist")
        currentUrl = url
        currentTitle = title
        currentArtist = artist

        // Report track played to remote logger
        AppLog.trackPlayed(url, title, artist)

        val mediaItem = MediaItem.Builder()
            .setUri(url)
            .setMediaMetadata(
                MediaMetadata.Builder()
                    .setTitle(title)
                    .setArtist(artist)
                    .build()
            )
            .build()

        player?.apply {
            setMediaItem(mediaItem)
            AppLog.d(TAG, "Media item set, preparing player")
            prepare()
            play()
            AppLog.d(TAG, "Playback started")
        }
        updatePlayerState()
        updateNotification()
    }

    fun play() {
        AppLog.d(TAG, "play() called")
        player?.play()
    }

    fun pause() {
        AppLog.d(TAG, "pause() called")
        player?.pause()
    }

    fun stop() {
        AppLog.d(TAG, "stop() called")
        player?.stop()
        player?.clearMediaItems()
        currentTitle = null
        currentArtist = null
        currentUrl = null
        updatePlayerState()
        updateNotification()
    }

    fun setVolume(level: Float) {
        val clampedLevel = level.coerceIn(0f, 1f)
        AppLog.d(TAG, "setVolume: $clampedLevel (requested: $level)")
        player?.volume = clampedLevel
        updatePlayerState()
    }

    fun getVolume(): Float = player?.volume ?: 1f

    fun setMuted(muted: Boolean) {
        AppLog.d(TAG, "setMuted: $muted")
        player?.volume = if (muted) 0f else 1f
        updatePlayerState()
    }

    fun isMuted(): Boolean = (player?.volume ?: 1f) == 0f

    fun toggleMute() {
        val newMuted = !isMuted()
        AppLog.d(TAG, "toggleMute: muted will be $newMuted")
        setMuted(newMuted)
    }

    fun seek(position: Long) {
        AppLog.d(TAG, "seek: position=$position ms")
        player?.seekTo(position)
        updatePlayerState()
    }

    fun getPosition(): Long = player?.currentPosition ?: 0

    fun getDuration(): Long = player?.duration ?: 0

    fun setUpdateHandler(handler: MediaHttpServer.UpdateHandler?) {
        httpServer?.updateHandler = handler
    }

    private var lastError: String? = null

    private fun handlePlaybackError(error: PlaybackException) {
        val errorMessage = when (error.errorCode) {
            PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_FAILED ->
                "Network connection failed"
            PlaybackException.ERROR_CODE_IO_NETWORK_CONNECTION_TIMEOUT ->
                "Network connection timeout"
            PlaybackException.ERROR_CODE_IO_BAD_HTTP_STATUS ->
                "Bad HTTP status (invalid URL or server error)"
            PlaybackException.ERROR_CODE_IO_FILE_NOT_FOUND ->
                "File not found"
            PlaybackException.ERROR_CODE_IO_INVALID_HTTP_CONTENT_TYPE ->
                "Invalid content type"
            PlaybackException.ERROR_CODE_PARSING_CONTAINER_MALFORMED ->
                "Invalid media format"
            PlaybackException.ERROR_CODE_PARSING_MANIFEST_MALFORMED ->
                "Invalid media manifest"
            PlaybackException.ERROR_CODE_DECODER_INIT_FAILED ->
                "Decoder initialization failed"
            PlaybackException.ERROR_CODE_UNSPECIFIED ->
                "Unknown playback error"
            else ->
                "Playback error: ${error.errorCodeName}"
        }

        AppLog.e(TAG, "Playback error handled: $errorMessage for URL: $currentUrl")
        lastError = errorMessage

        // Reset player state but keep it ready for next command
        player?.stop()
        player?.clearMediaItems()

        // Update state to idle with error info
        _playerState.value = PlayerState(
            state = PlayerState.STATE_IDLE,
            volume = player?.volume ?: 1f,
            muted = isMuted(),
            mediaTitle = "Error: $errorMessage",
            mediaArtist = null,
            mediaDuration = null,
            mediaPosition = null,
            mediaUrl = currentUrl,
            error = errorMessage
        )

        // Clear current media info
        currentTitle = null
        currentArtist = null

        // Notify clients
        httpServer?.broadcastState(_playerState.value)
        updateNotification()
    }

    fun getLastError(): String? = lastError

    fun clearError() {
        lastError = null
    }

    private fun updatePlayerState() {
        val state = when {
            player?.isPlaying == true -> PlayerState.STATE_PLAYING
            player?.playbackState == Player.STATE_BUFFERING -> PlayerState.STATE_BUFFERING
            player?.playbackState == Player.STATE_READY && player?.playWhenReady == false -> PlayerState.STATE_PAUSED
            else -> PlayerState.STATE_IDLE
        }

        val newState = PlayerState(
            state = state,
            volume = player?.volume ?: 1f,
            muted = isMuted(),
            mediaTitle = currentTitle,
            mediaArtist = currentArtist,
            mediaDuration = if (getDuration() > 0) getDuration() else null,
            mediaPosition = if (getPosition() > 0) getPosition() else null,
            mediaUrl = currentUrl
        )

        if (_playerState.value != newState) {
            AppLog.d(TAG, "State updated: state=$state, volume=${newState.volume}, muted=${newState.muted}, title=$currentTitle")
            _playerState.value = newState
        }

        // Notify WebSocket clients
        httpServer?.broadcastState(_playerState.value)
    }

    private fun createNotification(): Notification {
        val intent = Intent(this, MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val playPauseAction = if (player?.isPlaying == true) {
            NotificationCompat.Action(
                android.R.drawable.ic_media_pause,
                "Pause",
                createActionIntent(ACTION_PAUSE)
            )
        } else {
            NotificationCompat.Action(
                android.R.drawable.ic_media_play,
                "Play",
                createActionIntent(ACTION_PLAY)
            )
        }

        val stopAction = NotificationCompat.Action(
            android.R.drawable.ic_delete,
            "Stop",
            createActionIntent(ACTION_STOP)
        )

        val title = currentTitle ?: deviceName
        val text = when (_playerState.value.state) {
            PlayerState.STATE_PLAYING -> "Playing" + (currentArtist?.let { " - $it" } ?: "")
            PlayerState.STATE_PAUSED -> "Paused"
            PlayerState.STATE_BUFFERING -> "Buffering..."
            else -> "Ready"
        }

        return NotificationCompat.Builder(this, MediaPlayerApp.NOTIFICATION_CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentIntent(pendingIntent)
            .addAction(playPauseAction)
            .addAction(stopAction)
            .setStyle(
                androidx.media.app.NotificationCompat.MediaStyle()
                    .setMediaSession(mediaSession?.sessionToken)
                    .setShowActionsInCompactView(0, 1)
            )
            .setOngoing(true)
            .build()
    }

    private fun createActionIntent(action: String): PendingIntent {
        val intent = Intent(this, MediaPlayerService::class.java).apply {
            this.action = action
        }
        return PendingIntent.getService(
            this, action.hashCode(), intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    private fun updateNotification() {
        val notification = createNotification()
        val notificationManager = getSystemService(NOTIFICATION_SERVICE) as android.app.NotificationManager
        notificationManager.notify(NOTIFICATION_ID, notification)
    }

    override fun onDestroy() {
        AppLog.i(TAG, "Service destroying")
        serviceScope.cancel()
        httpServer?.stop()
        releaseWakeLocks()
        player?.release()
        mediaSession?.release()
        AppLog.i(TAG, "Service destroyed")
        super.onDestroy()
    }
}
