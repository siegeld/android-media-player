package com.example.androidmediaplayer.util

import android.content.Context
import android.os.Build
import android.provider.Settings
import kotlinx.coroutines.*
import org.json.JSONArray
import org.json.JSONObject
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.*
import java.util.concurrent.ConcurrentLinkedQueue

/**
 * Remote logger that sends logs and events to the monitoring server.
 */
class RemoteLogger(private val context: Context) {

    companion object {
        private const val TAG = "RemoteLogger"
        private const val BATCH_SIZE = 20
        private const val FLUSH_INTERVAL_MS = 5000L
        private const val CHECKIN_INTERVAL_MS = 60000L
    }

    private var serverUrl: String? = null
    private var deviceId: String = ""
    private var deviceName: String = "Android Media Player"
    private var appVersion: String = "unknown"

    private val logQueue = ConcurrentLinkedQueue<JSONObject>()
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var flushJob: Job? = null
    private var checkinJob: Job? = null

    private val dateFormat = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)

    fun configure(serverHost: String, port: Int = 8888, deviceName: String) {
        this.serverUrl = "http://$serverHost:$port"
        this.deviceName = deviceName

        // Generate a stable device ID
        this.deviceId = Settings.Secure.getString(context.contentResolver, Settings.Secure.ANDROID_ID)
            ?: UUID.randomUUID().toString()

        // Get app version
        try {
            val pInfo = context.packageManager.getPackageInfo(context.packageName, 0)
            appVersion = "${pInfo.versionName} (${
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) pInfo.longVersionCode
                else @Suppress("DEPRECATION") pInfo.versionCode
            })"
        } catch (e: Exception) {
            appVersion = "unknown"
        }

        AppLog.d(TAG, "Remote logger configured: server=$serverUrl, deviceId=$deviceId")

        startPeriodicTasks()
    }

    fun updateDeviceName(newName: String) {
        this.deviceName = newName
        AppLog.d(TAG, "Remote logger device name updated to: $newName")
    }

    private fun startPeriodicTasks() {
        // Start periodic log flush
        flushJob?.cancel()
        flushJob = scope.launch {
            while (isActive) {
                delay(FLUSH_INTERVAL_MS)
                flushLogs()
            }
        }

        // Start periodic check-in
        checkinJob?.cancel()
        checkinJob = scope.launch {
            while (isActive) {
                checkin()
                delay(CHECKIN_INTERVAL_MS)
            }
        }
    }

    fun stop() {
        flushJob?.cancel()
        checkinJob?.cancel()
        scope.launch {
            flushLogs()  // Final flush
        }
    }

    fun log(level: String, tag: String, message: String) {
        if (serverUrl == null) return

        val entry = JSONObject().apply {
            put("timestamp", dateFormat.format(Date()))
            put("level", level)
            put("tag", tag)
            put("message", message)
            put("device_id", deviceId)
            put("device_name", deviceName)
            put("app_version", appVersion)
        }

        logQueue.add(entry)

        // Flush immediately if queue is large or if it's an error
        if (logQueue.size >= BATCH_SIZE || level == "E") {
            scope.launch {
                flushLogs()
            }
        }
    }

    fun trackPlayed(url: String, title: String?, artist: String?) {
        if (serverUrl == null) return

        scope.launch {
            try {
                val trackInfo = JSONObject().apply {
                    put("device_id", deviceId)
                    put("device_name", deviceName)
                    put("url", url)
                    title?.let { put("title", it) }
                    artist?.let { put("artist", it) }
                }

                postJson("$serverUrl/track", trackInfo)
            } catch (e: Exception) {
                // Don't log to avoid infinite loop
            }
        }
    }

    private suspend fun checkin() {
        if (serverUrl == null) return

        try {
            val info = JSONObject().apply {
                put("device_id", deviceId)
                put("device_name", deviceName)
                put("app_version", appVersion)
                put("android_version", Build.VERSION.SDK_INT)
                put("device_model", "${Build.MANUFACTURER} ${Build.MODEL}")
            }

            postJson("$serverUrl/checkin", info)
        } catch (e: Exception) {
            // Silent fail for check-in
        }
    }

    private suspend fun flushLogs() {
        if (serverUrl == null || logQueue.isEmpty()) return

        val batch = mutableListOf<JSONObject>()
        while (batch.size < BATCH_SIZE * 2) {
            val entry = logQueue.poll() ?: break
            batch.add(entry)
        }

        if (batch.isEmpty()) return

        try {
            val jsonArray = JSONArray()
            batch.forEach { jsonArray.put(it) }

            postJson("$serverUrl/logs", jsonArray)
        } catch (e: Exception) {
            // Put entries back in queue for retry (at the front would be ideal but ConcurrentLinkedQueue doesn't support it)
            // For simplicity, we just lose these on failure
        }
    }

    private fun postJson(urlString: String, data: Any) {
        val url = URL(urlString)
        val connection = url.openConnection() as HttpURLConnection

        try {
            connection.requestMethod = "POST"
            connection.setRequestProperty("Content-Type", "application/json")
            connection.doOutput = true
            connection.connectTimeout = 5000
            connection.readTimeout = 5000

            OutputStreamWriter(connection.outputStream).use { writer ->
                writer.write(data.toString())
                writer.flush()
            }

            val responseCode = connection.responseCode
            if (responseCode != HttpURLConnection.HTTP_OK) {
                // Silent fail
            }
        } finally {
            connection.disconnect()
        }
    }
}
