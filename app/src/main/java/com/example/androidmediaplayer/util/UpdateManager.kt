package com.example.androidmediaplayer.util

import android.app.PendingIntent
import android.app.admin.DevicePolicyManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import com.example.androidmediaplayer.receiver.DeviceAdminReceiver
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL

class UpdateManager(private val context: Context) {

    companion object {
        private const val TAG = "UpdateManager"
        private const val UPDATE_FILE_NAME = "update.apk"
    }

    data class UpdateInfo(
        val available: Boolean,
        val versionCode: Int,
        val versionName: String,
        val size: Long,
        val isNewer: Boolean
    )

    sealed class UpdateState {
        object Idle : UpdateState()
        object Checking : UpdateState()
        data class UpdateAvailable(val info: UpdateInfo) : UpdateState()
        object NoUpdate : UpdateState()
        data class Downloading(val progress: Int) : UpdateState()
        object Installing : UpdateState()
        data class Error(val message: String) : UpdateState()
    }

    private val _state = MutableStateFlow<UpdateState>(UpdateState.Idle)
    val state: StateFlow<UpdateState> = _state.asStateFlow()

    private var updateServerUrl: String = ""

    fun setUpdateServer(host: String, port: Int = 8888) {
        updateServerUrl = "http://$host:$port"
        AppLog.d(TAG, "Update server set to: $updateServerUrl")
    }

    fun getCurrentVersionCode(): Int {
        return try {
            val pInfo = context.packageManager.getPackageInfo(context.packageName, 0)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                pInfo.longVersionCode.toInt()
            } else {
                @Suppress("DEPRECATION")
                pInfo.versionCode
            }
        } catch (e: PackageManager.NameNotFoundException) {
            AppLog.e(TAG, "Failed to get version code", e)
            0
        }
    }

    fun getCurrentVersionName(): String {
        return try {
            val pInfo = context.packageManager.getPackageInfo(context.packageName, 0)
            pInfo.versionName ?: "unknown"
        } catch (e: PackageManager.NameNotFoundException) {
            AppLog.e(TAG, "Failed to get version name", e)
            "unknown"
        }
    }

    suspend fun checkForUpdate(): UpdateInfo? {
        if (updateServerUrl.isEmpty()) {
            AppLog.w(TAG, "Update server URL not set")
            _state.value = UpdateState.Error("Update server not configured")
            return null
        }

        _state.value = UpdateState.Checking
        AppLog.i(TAG, "Checking for updates at $updateServerUrl/version")

        return withContext(Dispatchers.IO) {
            try {
                val url = URL("$updateServerUrl/version")
                val connection = url.openConnection() as HttpURLConnection
                connection.connectTimeout = 10000
                connection.readTimeout = 10000

                try {
                    val responseCode = connection.responseCode
                    if (responseCode != HttpURLConnection.HTTP_OK) {
                        AppLog.e(TAG, "Server returned HTTP $responseCode")
                        _state.value = UpdateState.Error("Server error: HTTP $responseCode")
                        return@withContext null
                    }

                    val response = connection.inputStream.bufferedReader().use { it.readText() }
                    AppLog.d(TAG, "Version response: $response")

                    val json = JSONObject(response)

                    if (!json.optBoolean("available", false)) {
                        AppLog.w(TAG, "No update available on server")
                        _state.value = UpdateState.NoUpdate
                        return@withContext null
                    }

                    val serverVersionCode = json.getInt("versionCode")
                    val serverVersionName = json.getString("versionName")
                    val size = json.getLong("size")
                    val currentVersionCode = getCurrentVersionCode()

                    val isNewer = serverVersionCode > currentVersionCode

                    AppLog.i(TAG, "Server version: $serverVersionName ($serverVersionCode), " +
                            "Current: ${getCurrentVersionName()} ($currentVersionCode), " +
                            "isNewer: $isNewer")

                    val info = UpdateInfo(
                        available = true,
                        versionCode = serverVersionCode,
                        versionName = serverVersionName,
                        size = size,
                        isNewer = isNewer
                    )

                    if (isNewer) {
                        _state.value = UpdateState.UpdateAvailable(info)
                    } else {
                        _state.value = UpdateState.NoUpdate
                    }

                    info
                } finally {
                    connection.disconnect()
                }
            } catch (e: Exception) {
                AppLog.e(TAG, "Failed to check for update: ${e.message}", e)
                _state.value = UpdateState.Error("Failed to check: ${e.message}")
                null
            }
        }
    }

    suspend fun downloadAndInstall(): Boolean {
        if (updateServerUrl.isEmpty()) {
            _state.value = UpdateState.Error("Update server not configured")
            return false
        }

        AppLog.i(TAG, "Starting update download from $updateServerUrl/apk")

        return withContext(Dispatchers.IO) {
            try {
                _state.value = UpdateState.Downloading(0)

                val url = URL("$updateServerUrl/apk")
                val connection = url.openConnection() as HttpURLConnection
                connection.connectTimeout = 30000
                connection.readTimeout = 60000

                try {
                    val responseCode = connection.responseCode
                    if (responseCode != HttpURLConnection.HTTP_OK) {
                        AppLog.e(TAG, "Download failed: HTTP $responseCode")
                        _state.value = UpdateState.Error("Download failed: HTTP $responseCode")
                        return@withContext false
                    }

                    val contentLength = connection.contentLength
                    AppLog.d(TAG, "Download size: $contentLength bytes")

                    // Create updates directory
                    val updatesDir = File(context.getExternalFilesDir(null), "updates")
                    if (!updatesDir.exists()) {
                        updatesDir.mkdirs()
                    }

                    val apkFile = File(updatesDir, UPDATE_FILE_NAME)

                    // Download the file
                    connection.inputStream.use { input ->
                        FileOutputStream(apkFile).use { output ->
                            val buffer = ByteArray(8192)
                            var totalBytesRead = 0L
                            var bytesRead: Int

                            while (input.read(buffer).also { bytesRead = it } != -1) {
                                output.write(buffer, 0, bytesRead)
                                totalBytesRead += bytesRead

                                if (contentLength > 0) {
                                    val progress = ((totalBytesRead * 100) / contentLength).toInt()
                                    _state.value = UpdateState.Downloading(progress)
                                }
                            }
                        }
                    }

                    AppLog.i(TAG, "Download complete: ${apkFile.absolutePath} (${apkFile.length()} bytes)")

                    // Verify the file exists and has content
                    if (!apkFile.exists() || apkFile.length() == 0L) {
                        AppLog.e(TAG, "Downloaded file is invalid")
                        _state.value = UpdateState.Error("Downloaded file is invalid")
                        return@withContext false
                    }

                    // Trigger installation
                    _state.value = UpdateState.Installing
                    withContext(Dispatchers.Main) {
                        installApk(apkFile)
                    }

                    true
                } finally {
                    connection.disconnect()
                }
            } catch (e: Exception) {
                AppLog.e(TAG, "Download failed: ${e.message}", e)
                _state.value = UpdateState.Error("Download failed: ${e.message}")
                false
            }
        }
    }

    private fun installApk(apkFile: File) {
        AppLog.i(TAG, "Installing APK: ${apkFile.absolutePath}")

        // Try silent install if we're device owner
        if (isDeviceOwner()) {
            AppLog.i(TAG, "Device owner mode - attempting silent install")
            if (silentInstall(apkFile)) {
                return
            }
            AppLog.w(TAG, "Silent install failed, falling back to standard install")
        }

        // Standard install with user prompt
        try {
            val uri = FileProvider.getUriForFile(
                context,
                "${context.packageName}.fileprovider",
                apkFile
            )

            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, "application/vnd.android.package-archive")
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }

            context.startActivity(intent)
            AppLog.i(TAG, "Install intent launched")
        } catch (e: Exception) {
            AppLog.e(TAG, "Failed to launch installer: ${e.message}", e)
            _state.value = UpdateState.Error("Failed to install: ${e.message}")
        }
    }

    fun isDeviceOwner(): Boolean {
        val dpm = context.getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager
        return dpm.isDeviceOwnerApp(context.packageName)
    }

    private fun silentInstall(apkFile: File): Boolean {
        try {
            val packageInstaller = context.packageManager.packageInstaller
            val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
            params.setAppPackageName(context.packageName)

            val sessionId = packageInstaller.createSession(params)
            val session = packageInstaller.openSession(sessionId)

            // Write the APK to the session
            session.openWrite("update.apk", 0, apkFile.length()).use { outputStream ->
                apkFile.inputStream().use { inputStream ->
                    inputStream.copyTo(outputStream)
                }
                session.fsync(outputStream)
            }

            // Create a pending intent for the install result
            val intent = Intent(context, DeviceAdminReceiver::class.java)
            intent.action = "com.example.androidmediaplayer.INSTALL_COMPLETE"
            val pendingIntent = PendingIntent.getBroadcast(
                context,
                sessionId,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
            )

            // Commit the session
            session.commit(pendingIntent.intentSender)
            AppLog.i(TAG, "Silent install session committed")
            return true
        } catch (e: Exception) {
            AppLog.e(TAG, "Silent install failed: ${e.message}", e)
            return false
        }
    }

    fun canInstallPackages(): Boolean {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.packageManager.canRequestPackageInstalls()
        } else {
            true
        }
    }

    fun getInstallPermissionIntent(): Intent {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES).apply {
                data = Uri.parse("package:${context.packageName}")
            }
        } else {
            Intent(Settings.ACTION_SECURITY_SETTINGS)
        }
    }

    fun resetState() {
        _state.value = UpdateState.Idle
    }
}
