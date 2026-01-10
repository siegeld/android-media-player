package com.example.androidmediaplayer.util

import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.text.SimpleDateFormat
import java.util.*

/**
 * Centralized logging utility that captures logs for display in the UI.
 * Also forwards to Android's Log for logcat output and optionally to a remote server.
 */
object AppLog {
    private const val MAX_LOG_ENTRIES = 500

    private val _logs = MutableStateFlow<List<LogEntry>>(emptyList())
    val logs: StateFlow<List<LogEntry>> = _logs.asStateFlow()

    private val _enabled = MutableStateFlow(false)
    val enabled: StateFlow<Boolean> = _enabled.asStateFlow()

    private val dateFormat = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)

    // Remote logger instance (optional)
    private var remoteLogger: RemoteLogger? = null
    private var remoteLoggingEnabled = false

    data class LogEntry(
        val timestamp: String,
        val level: String,
        val tag: String,
        val message: String
    ) {
        override fun toString(): String = "$timestamp $level/$tag: $message"
    }

    fun setRemoteLogger(logger: RemoteLogger?) {
        remoteLogger = logger
    }

    fun setRemoteLoggingEnabled(enabled: Boolean) {
        remoteLoggingEnabled = enabled
    }

    fun setEnabled(enabled: Boolean) {
        _enabled.value = enabled
        if (enabled) {
            addEntry("I", "AppLog", "Logging enabled", sendRemote = false)
        }
    }

    fun isEnabled(): Boolean = _enabled.value

    fun clear() {
        _logs.value = emptyList()
    }

    private fun addEntry(level: String, tag: String, message: String, sendRemote: Boolean = true) {
        // Add to local UI log if enabled
        if (_enabled.value) {
            val entry = LogEntry(
                timestamp = dateFormat.format(Date()),
                level = level,
                tag = tag,
                message = message
            )

            val currentLogs = _logs.value.toMutableList()
            currentLogs.add(entry)

            // Trim if exceeds max
            if (currentLogs.size > MAX_LOG_ENTRIES) {
                _logs.value = currentLogs.takeLast(MAX_LOG_ENTRIES)
            } else {
                _logs.value = currentLogs
            }
        }

        // Send to remote logger if enabled
        if (sendRemote && remoteLoggingEnabled) {
            remoteLogger?.log(level, tag, message)
        }
    }

    fun v(tag: String, message: String) {
        Log.v(tag, message)
        addEntry("V", tag, message)
    }

    fun d(tag: String, message: String) {
        Log.d(tag, message)
        addEntry("D", tag, message)
    }

    fun i(tag: String, message: String) {
        Log.i(tag, message)
        addEntry("I", tag, message)
    }

    fun w(tag: String, message: String) {
        Log.w(tag, message)
        addEntry("W", tag, message)
    }

    fun e(tag: String, message: String, throwable: Throwable? = null) {
        if (throwable != null) {
            Log.e(tag, message, throwable)
            addEntry("E", tag, "$message: ${throwable.message}")
        } else {
            Log.e(tag, message)
            addEntry("E", tag, message)
        }
    }

    // Helper to report track played (delegates to remote logger)
    fun trackPlayed(url: String, title: String?, artist: String?) {
        if (remoteLoggingEnabled) {
            remoteLogger?.trackPlayed(url, title, artist)
        }
    }
}
