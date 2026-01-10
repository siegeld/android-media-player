package com.example.androidmediaplayer

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import com.example.androidmediaplayer.util.AppLog
import com.example.androidmediaplayer.util.RemoteLogger

class MediaPlayerApp : Application() {

    companion object {
        const val NOTIFICATION_CHANNEL_ID = "media_playback_channel"
        const val DEFAULT_UPDATE_SERVER_PORT = 9742

        // Singleton remote logger
        var remoteLogger: RemoteLogger? = null
            private set
    }

    private val prefs by lazy {
        getSharedPreferences("media_player_prefs", Context.MODE_PRIVATE)
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        setupRemoteLogging()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            NOTIFICATION_CHANNEL_ID,
            getString(R.string.notification_channel_name),
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = getString(R.string.notification_channel_description)
            setShowBadge(false)
        }

        val notificationManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        notificationManager.createNotificationChannel(channel)
    }

    private fun setupRemoteLogging() {
        remoteLogger = RemoteLogger(this)
        AppLog.setRemoteLogger(remoteLogger)

        // Check if we have a configured update server
        val serverHost = prefs.getString("update_server_host", null)
        val deviceName = prefs.getString("device_name", "Android Media Player") ?: "Android Media Player"

        if (serverHost != null) {
            remoteLogger?.configure(serverHost, DEFAULT_UPDATE_SERVER_PORT, deviceName)
            AppLog.setRemoteLoggingEnabled(true)
        }
    }

    fun configureRemoteLogging(serverHost: String, deviceName: String) {
        remoteLogger?.configure(serverHost, DEFAULT_UPDATE_SERVER_PORT, deviceName)
        AppLog.setRemoteLoggingEnabled(true)
    }
}
