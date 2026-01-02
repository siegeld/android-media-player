package com.example.androidmediaplayer.receiver

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.core.content.ContextCompat
import com.example.androidmediaplayer.service.MediaPlayerService

class BootReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "BootReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED ||
            intent.action == "android.intent.action.QUICKBOOT_POWERON") {

            Log.i(TAG, "Boot completed, checking if service should auto-start")

            val prefs = context.getSharedPreferences("media_player_prefs", Context.MODE_PRIVATE)
            val shouldAutoStart = prefs.getBoolean("service_running", false)

            if (shouldAutoStart) {
                val deviceName = prefs.getString("device_name", "Android Media Player")
                val port = prefs.getInt("port", 8765)

                Log.i(TAG, "Auto-starting media service: deviceName=$deviceName, port=$port")

                val serviceIntent = Intent(context, MediaPlayerService::class.java).apply {
                    putExtra(MediaPlayerService.EXTRA_PORT, port)
                    putExtra(MediaPlayerService.EXTRA_DEVICE_NAME, deviceName)
                }

                try {
                    ContextCompat.startForegroundService(context, serviceIntent)
                    Log.i(TAG, "Service start initiated successfully")
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to auto-start service: ${e.message}", e)
                }
            } else {
                Log.d(TAG, "Service was not running before reboot, not auto-starting")
            }
        }
    }
}
