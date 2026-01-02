package com.example.androidmediaplayer.service

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Binder
import android.os.IBinder
import android.support.v4.media.session.MediaSessionCompat
import android.util.Log
import androidx.core.app.NotificationCompat
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
        Log.d(TAG, "Service bound")
        return binder
    }

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "Service created")
        initializePlayer()
        initializeMediaSession()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.d(TAG, "onStartCommand: action=${intent?.action}")
        when (intent?.action) {
            ACTION_PLAY -> {
                Log.d(TAG, "Received ACTION_PLAY from notification")
                play()
            }
            ACTION_PAUSE -> {
                Log.d(TAG, "Received ACTION_PAUSE from notification")
                pause()
            }
            ACTION_STOP -> {
                Log.d(TAG, "Received ACTION_STOP from notification")
                stop()
            }
            else -> {
                val port = intent?.getIntExtra(EXTRA_PORT, 8765) ?: 8765
                deviceName = intent?.getStringExtra(EXTRA_DEVICE_NAME) ?: "Android Media Player"
                Log.i(TAG, "Starting foreground service: deviceName=$deviceName, port=$port")
                startForegroundService(port)
            }
        }
        return START_STICKY
    }

    private fun startForegroundService(port: Int) {
        Log.d(TAG, "Starting foreground with notification")
        startForeground(NOTIFICATION_ID, createNotification())
        startHttpServer(port)
    }

    private fun initializePlayer() {
        Log.d(TAG, "Initializing ExoPlayer")
        player = ExoPlayer.Builder(this).build().apply {
            addListener(object : Player.Listener {
                override fun onPlaybackStateChanged(playbackState: Int) {
                    val stateStr = when (playbackState) {
                        Player.STATE_IDLE -> "IDLE"
                        Player.STATE_BUFFERING -> "BUFFERING"
                        Player.STATE_READY -> "READY"
                        Player.STATE_ENDED -> "ENDED"
                        else -> "UNKNOWN($playbackState)"
                    }
                    Log.d(TAG, "Playback state changed: $stateStr")
                    updatePlayerState()
                }

                override fun onIsPlayingChanged(isPlaying: Boolean) {
                    Log.d(TAG, "isPlaying changed: $isPlaying")
                    updatePlayerState()
                    updateNotification()
                }

                override fun onMediaMetadataChanged(mediaMetadata: MediaMetadata) {
                    Log.d(TAG, "Media metadata changed: title=${mediaMetadata.title}, artist=${mediaMetadata.artist}, displayTitle=${mediaMetadata.displayTitle}")
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
                    Log.d(TAG, "onMetadata: ${metadata.length()} entries")
                    for (i in 0 until metadata.length()) {
                        val entry = metadata.get(i)
                        Log.d(TAG, "Metadata entry $i: ${entry::class.simpleName} = $entry")
                        if (entry is IcyInfo) {
                            Log.i(TAG, "ICY metadata: title=${entry.title}, url=${entry.url}")
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
                    Log.e(TAG, "Player error: ${error.errorCodeName} - ${error.message}", error)
                    handlePlaybackError(error)
                }
            })
        }
        Log.i(TAG, "ExoPlayer initialized successfully")
    }

    private fun initializeMediaSession() {
        Log.d(TAG, "Initializing MediaSession")
        mediaSession = MediaSessionCompat(this, "MediaPlayerService").apply {
            isActive = true
        }
        Log.i(TAG, "MediaSession initialized")
    }

    private fun startHttpServer(port: Int) {
        Log.i(TAG, "Starting HTTP server on port $port")
        serviceScope.launch(Dispatchers.IO) {
            try {
                httpServer = MediaHttpServer(this@MediaPlayerService, port, deviceName)
                httpServer?.start()
                Log.i(TAG, "HTTP server started successfully on port $port")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to start HTTP server: ${e.message}", e)
            }
        }
    }

    fun playMedia(url: String, title: String? = null, artist: String? = null) {
        Log.i(TAG, "playMedia: url=$url, title=$title, artist=$artist")
        currentUrl = url
        currentTitle = title
        currentArtist = artist

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
            Log.d(TAG, "Media item set, preparing player")
            prepare()
            play()
            Log.d(TAG, "Playback started")
        }
        updatePlayerState()
        updateNotification()
    }

    fun play() {
        Log.d(TAG, "play() called")
        player?.play()
    }

    fun pause() {
        Log.d(TAG, "pause() called")
        player?.pause()
    }

    fun stop() {
        Log.d(TAG, "stop() called")
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
        Log.d(TAG, "setVolume: $clampedLevel (requested: $level)")
        player?.volume = clampedLevel
        updatePlayerState()
    }

    fun getVolume(): Float = player?.volume ?: 1f

    fun setMuted(muted: Boolean) {
        Log.d(TAG, "setMuted: $muted")
        player?.volume = if (muted) 0f else 1f
        updatePlayerState()
    }

    fun isMuted(): Boolean = (player?.volume ?: 1f) == 0f

    fun toggleMute() {
        val newMuted = !isMuted()
        Log.d(TAG, "toggleMute: muted will be $newMuted")
        setMuted(newMuted)
    }

    fun seek(position: Long) {
        Log.d(TAG, "seek: position=$position ms")
        player?.seekTo(position)
        updatePlayerState()
    }

    fun getPosition(): Long = player?.currentPosition ?: 0

    fun getDuration(): Long = player?.duration ?: 0

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

        Log.e(TAG, "Playback error handled: $errorMessage for URL: $currentUrl")
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
            Log.d(TAG, "State updated: state=$state, volume=${newState.volume}, muted=${newState.muted}, title=$currentTitle")
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
        Log.i(TAG, "Service destroying")
        serviceScope.cancel()
        httpServer?.stop()
        player?.release()
        mediaSession?.release()
        Log.i(TAG, "Service destroyed")
        super.onDestroy()
    }
}
