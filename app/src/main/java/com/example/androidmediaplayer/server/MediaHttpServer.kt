package com.example.androidmediaplayer.server

import com.example.androidmediaplayer.model.*
import com.example.androidmediaplayer.service.MediaPlayerService
import com.example.androidmediaplayer.util.AppLog
import com.google.gson.Gson
import io.ktor.http.*
import io.ktor.serialization.gson.*
import io.ktor.server.application.*
import io.ktor.server.engine.*
import io.ktor.server.netty.*
import io.ktor.server.plugins.contentnegotiation.*
import io.ktor.server.plugins.cors.routing.*
import io.ktor.server.request.*
import io.ktor.server.response.*
import io.ktor.server.routing.*
import io.ktor.server.websocket.*
import io.ktor.websocket.*
import kotlinx.coroutines.*
import java.time.Duration
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger

class MediaHttpServer(
    private val service: MediaPlayerService,
    private val port: Int,
    private var deviceName: String
) {
    companion object {
        private const val TAG = "MediaHttpServer"
    }

    interface UpdateHandler {
        suspend fun checkForUpdate(): UpdateInfo?
        suspend fun downloadAndInstall(): Boolean
        fun getUpdateState(): String
    }

    interface NameChangeHandler {
        fun onNameChanged(newName: String)
    }

    data class UpdateInfo(
        val versionName: String,
        val versionCode: Int,
        val isNewer: Boolean,
        val currentVersion: String,
        val currentCode: Int
    )

    private var server: NettyApplicationEngine? = null
    private val gson = Gson()
    private val wsConnections = ConcurrentHashMap.newKeySet<WebSocketSession>()
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val connectionCounter = AtomicInteger(0)

    var updateHandler: UpdateHandler? = null
    var nameChangeHandler: NameChangeHandler? = null

    fun start() {
        AppLog.i(TAG, "Starting HTTP/WebSocket server on port $port for device '$deviceName'")
        server = embeddedServer(Netty, port = port, configure = {
            // Connection timeout settings to prevent CLOSE_WAIT socket accumulation
            connectionGroupSize = 2
            workerGroupSize = 4
            callGroupSize = 8
            // Timeout for reading request (prevents stale connections)
            requestReadTimeoutSeconds = 30
            // Timeout for writing response
            responseWriteTimeoutSeconds = 30
        }) {
            install(ContentNegotiation) {
                gson {
                    setPrettyPrinting()
                }
            }
            install(CORS) {
                anyHost()
                allowHeader(HttpHeaders.ContentType)
                allowMethod(HttpMethod.Get)
                allowMethod(HttpMethod.Post)
                allowMethod(HttpMethod.Options)
            }
            install(WebSockets) {
                pingPeriod = Duration.ofSeconds(15)
                timeout = Duration.ofSeconds(30)
                maxFrameSize = Long.MAX_VALUE
                masking = false
            }
            configureRouting()
        }.start(wait = false)
        AppLog.i(TAG, "Server started successfully on port $port")
    }

    fun stop() {
        AppLog.i(TAG, "Stopping HTTP server, closing ${wsConnections.size} WebSocket connections")
        scope.cancel()
        wsConnections.forEach { session ->
            scope.launch {
                try {
                    session.close(CloseReason(CloseReason.Codes.GOING_AWAY, "Server shutting down"))
                } catch (e: Exception) {
                    AppLog.w(TAG, "Error closing WebSocket session: ${e.message}")
                }
            }
        }
        server?.stop(1000, 2000)
        AppLog.i(TAG, "HTTP server stopped")
    }

    fun broadcastState(state: PlayerState) {
        if (wsConnections.isEmpty()) return

        val json = gson.toJson(state)
        AppLog.v(TAG, "Broadcasting state to ${wsConnections.size} clients: state=${state.state}")
        wsConnections.forEach { session ->
            scope.launch {
                try {
                    session.send(Frame.Text(json))
                } catch (e: Exception) {
                    AppLog.w(TAG, "Failed to send to WebSocket client, removing: ${e.message}")
                    wsConnections.remove(session)
                }
            }
        }
    }

    private fun Application.configureRouting() {
        routing {
            // Device info
            get("/") {
                val clientIp = call.request.local.remoteHost
                AppLog.d(TAG, "GET / from $clientIp - returning device info")
                val packageInfo = service.packageManager.getPackageInfo(service.packageName, 0)
                call.respond(mapOf(
                    "name" to deviceName,
                    "type" to "android_media_player",
                    "version" to packageInfo.versionName,
                    "capabilities" to listOf("play", "pause", "stop", "volume", "seek")
                ))
            }

            // Get current state with fresh position
            get("/state") {
                val clientIp = call.request.local.remoteHost
                AppLog.d(TAG, "GET /state from $clientIp")
                // Return state with fresh position/duration from player (must access ExoPlayer on Main thread)
                val freshState = kotlinx.coroutines.withContext(Dispatchers.Main) {
                    val state = service.playerState.value
                    val position = service.getPosition()
                    val duration = service.getDuration()
                    // ExoPlayer returns TIME_UNSET (Long.MIN_VALUE + 1) when unknown - convert to null
                    val validPosition = if (position >= 0) position else null
                    val validDuration = if (duration > 0) duration else null
                    state.copy(
                        mediaPosition = validPosition,
                        mediaDuration = validDuration
                    )
                }
                call.respond(freshState)
            }

            // Play media
            post("/play") {
                val clientIp = call.request.local.remoteHost
                try {
                    val contentType = call.request.contentType()
                    if (contentType == ContentType.Application.Json) {
                        val request = call.receive<PlayMediaRequest>()
                        AppLog.i(TAG, "POST /play from $clientIp - url=${request.url}, title=${request.title}")
                        kotlinx.coroutines.withContext(Dispatchers.Main) {
                            service.playMedia(request.url, request.title, request.artist)
                        }
                        call.respond(ApiResponse(success = true, message = "Playing", state = service.playerState.value))
                    } else {
                        AppLog.d(TAG, "POST /play from $clientIp - resume playback")
                        kotlinx.coroutines.withContext(Dispatchers.Main) {
                            service.play()
                        }
                        call.respond(ApiResponse(success = true, message = "Resumed", state = service.playerState.value))
                    }
                } catch (e: Exception) {
                    AppLog.e(TAG, "POST /play error from $clientIp: ${e.message}", e)
                    call.respond(HttpStatusCode.BadRequest, ApiResponse(success = false, message = e.message))
                }
            }

            // Pause
            post("/pause") {
                val clientIp = call.request.local.remoteHost
                AppLog.d(TAG, "POST /pause from $clientIp")
                kotlinx.coroutines.withContext(Dispatchers.Main) {
                    service.pause()
                }
                call.respond(ApiResponse(success = true, message = "Paused", state = service.playerState.value))
            }

            // Stop
            post("/stop") {
                val clientIp = call.request.local.remoteHost
                AppLog.d(TAG, "POST /stop from $clientIp")
                kotlinx.coroutines.withContext(Dispatchers.Main) {
                    service.stop()
                }
                call.respond(ApiResponse(success = true, message = "Stopped", state = service.playerState.value))
            }

            // Set volume
            post("/volume") {
                val clientIp = call.request.local.remoteHost
                try {
                    val request = call.receive<VolumeRequest>()
                    AppLog.d(TAG, "POST /volume from $clientIp - level=${request.level}")
                    kotlinx.coroutines.withContext(Dispatchers.Main) {
                        service.setVolume(request.level)
                    }
                    call.respond(ApiResponse(success = true, message = "Volume set", state = service.playerState.value))
                } catch (e: Exception) {
                    AppLog.e(TAG, "POST /volume error from $clientIp: ${e.message}", e)
                    call.respond(HttpStatusCode.BadRequest, ApiResponse(success = false, message = e.message))
                }
            }

            // Mute/unmute
            post("/mute") {
                val clientIp = call.request.local.remoteHost
                try {
                    val contentType = call.request.contentType()
                    if (contentType == ContentType.Application.Json) {
                        val request = call.receive<MuteRequest>()
                        AppLog.d(TAG, "POST /mute from $clientIp - muted=${request.muted}")
                        kotlinx.coroutines.withContext(Dispatchers.Main) {
                            if (request.muted != null) {
                                service.setMuted(request.muted)
                            } else {
                                service.toggleMute()
                            }
                        }
                    } else {
                        AppLog.d(TAG, "POST /mute from $clientIp - toggle")
                        kotlinx.coroutines.withContext(Dispatchers.Main) {
                            service.toggleMute()
                        }
                    }
                    call.respond(ApiResponse(success = true, message = "Mute toggled", state = service.playerState.value))
                } catch (e: Exception) {
                    AppLog.e(TAG, "POST /mute error from $clientIp: ${e.message}", e)
                    call.respond(HttpStatusCode.BadRequest, ApiResponse(success = false, message = e.message))
                }
            }

            // Seek
            post("/seek") {
                val clientIp = call.request.local.remoteHost
                try {
                    val request = call.receive<SeekRequest>()
                    AppLog.d(TAG, "POST /seek from $clientIp - position=${request.position}ms")
                    kotlinx.coroutines.withContext(Dispatchers.Main) {
                        service.seek(request.position)
                    }
                    call.respond(ApiResponse(success = true, message = "Seeked", state = service.playerState.value))
                } catch (e: Exception) {
                    AppLog.e(TAG, "POST /seek error from $clientIp: ${e.message}", e)
                    call.respond(HttpStatusCode.BadRequest, ApiResponse(success = false, message = e.message))
                }
            }

            // Check for updates
            get("/update") {
                val clientIp = call.request.local.remoteHost
                AppLog.d(TAG, "GET /update from $clientIp")
                val handler = updateHandler
                if (handler == null) {
                    call.respond(mapOf("success" to false, "message" to "Update handler not configured"))
                    return@get
                }
                try {
                    val info = handler.checkForUpdate()
                    if (info != null) {
                        call.respond(mapOf(
                            "success" to true,
                            "updateAvailable" to info.isNewer,
                            "currentVersion" to info.currentVersion,
                            "currentCode" to info.currentCode,
                            "availableVersion" to info.versionName,
                            "availableCode" to info.versionCode,
                            "state" to handler.getUpdateState()
                        ))
                    } else {
                        call.respond(mapOf(
                            "success" to false,
                            "message" to "Failed to check for updates",
                            "state" to handler.getUpdateState()
                        ))
                    }
                } catch (e: Exception) {
                    AppLog.e(TAG, "GET /update error: ${e.message}", e)
                    call.respond(mapOf("success" to false, "message" to e.message))
                }
            }

            // Trigger update installation
            post("/update") {
                val clientIp = call.request.local.remoteHost
                AppLog.i(TAG, "POST /update from $clientIp - triggering update")
                val handler = updateHandler
                if (handler == null) {
                    call.respond(mapOf("success" to false, "message" to "Update handler not configured"))
                    return@post
                }
                try {
                    // First check if update is available
                    val info = handler.checkForUpdate()
                    if (info == null || !info.isNewer) {
                        call.respond(mapOf(
                            "success" to false,
                            "message" to "No update available",
                            "state" to handler.getUpdateState()
                        ))
                        return@post
                    }
                    // Trigger download and install
                    val result = handler.downloadAndInstall()
                    call.respond(mapOf(
                        "success" to result,
                        "message" to if (result) "Update started" else "Update failed",
                        "version" to info.versionName,
                        "state" to handler.getUpdateState()
                    ))
                } catch (e: Exception) {
                    AppLog.e(TAG, "POST /update error: ${e.message}", e)
                    call.respond(mapOf("success" to false, "message" to e.message))
                }
            }

            // Get current device name
            get("/name") {
                val clientIp = call.request.local.remoteHost
                AppLog.d(TAG, "GET /name from $clientIp")
                call.respond(mapOf("name" to deviceName))
            }

            // Change device name
            post("/name") {
                val clientIp = call.request.local.remoteHost
                try {
                    val request = call.receive<NameRequest>()
                    AppLog.i(TAG, "POST /name from $clientIp - new name: ${request.name}")

                    val newName = request.name.trim()
                    if (newName.isEmpty()) {
                        call.respond(HttpStatusCode.BadRequest, mapOf("success" to false, "message" to "Name cannot be empty"))
                        return@post
                    }

                    deviceName = newName
                    nameChangeHandler?.onNameChanged(newName)

                    call.respond(mapOf("success" to true, "name" to deviceName, "message" to "Name changed successfully"))
                } catch (e: Exception) {
                    AppLog.e(TAG, "POST /name error from $clientIp: ${e.message}", e)
                    call.respond(HttpStatusCode.BadRequest, mapOf("success" to false, "message" to e.message))
                }
            }

            // WebSocket for real-time state updates
            webSocket("/ws") {
                val connectionId = connectionCounter.incrementAndGet()
                val clientInfo = "${call.request.local.remoteHost}:${call.request.local.remotePort}"
                AppLog.i(TAG, "WebSocket client #$connectionId connected from $clientInfo")
                wsConnections.add(this)

                try {
                    // Send current state on connect
                    val initialState = gson.toJson(service.playerState.value)
                    AppLog.d(TAG, "Sending initial state to client #$connectionId")
                    send(Frame.Text(initialState))

                    // Keep connection alive and handle incoming messages
                    for (frame in incoming) {
                        when (frame) {
                            is Frame.Text -> {
                                val text = frame.readText()
                                AppLog.d(TAG, "WebSocket command from client #$connectionId: $text")
                                handleWebSocketCommand(text, connectionId)
                            }
                            is Frame.Ping -> {
                                AppLog.v(TAG, "Ping from client #$connectionId")
                                send(Frame.Pong(frame.data))
                            }
                            else -> {}
                        }
                    }
                } catch (e: Exception) {
                    AppLog.w(TAG, "WebSocket error for client #$connectionId: ${e.message}")
                } finally {
                    wsConnections.remove(this)
                    AppLog.i(TAG, "WebSocket client #$connectionId disconnected, ${wsConnections.size} clients remaining")
                }
            }
        }
    }

    private suspend fun handleWebSocketCommand(text: String, connectionId: Int) {
        try {
            val command = gson.fromJson(text, Map::class.java)
            val cmdName = command["command"]
            AppLog.d(TAG, "Processing WebSocket command '$cmdName' from client #$connectionId")

            when (cmdName) {
                "play" -> {
                    val url = command["url"] as? String
                    if (url != null) {
                        AppLog.i(TAG, "WS play command: url=$url, title=${command["title"]}")
                        kotlinx.coroutines.withContext(Dispatchers.Main) {
                            service.playMedia(
                                url,
                                command["title"] as? String,
                                command["artist"] as? String
                            )
                        }
                    } else {
                        AppLog.d(TAG, "WS play command: resume")
                        kotlinx.coroutines.withContext(Dispatchers.Main) {
                            service.play()
                        }
                    }
                }
                "pause" -> {
                    AppLog.d(TAG, "WS pause command")
                    kotlinx.coroutines.withContext(Dispatchers.Main) { service.pause() }
                }
                "stop" -> {
                    AppLog.d(TAG, "WS stop command")
                    kotlinx.coroutines.withContext(Dispatchers.Main) { service.stop() }
                }
                "volume" -> {
                    val level = (command["level"] as? Number)?.toFloat()
                    if (level != null) {
                        AppLog.d(TAG, "WS volume command: level=$level")
                        kotlinx.coroutines.withContext(Dispatchers.Main) { service.setVolume(level) }
                    } else {
                        AppLog.w(TAG, "WS volume command missing 'level' parameter")
                    }
                }
                "mute" -> {
                    val muted = command["muted"] as? Boolean
                    AppLog.d(TAG, "WS mute command: muted=$muted")
                    kotlinx.coroutines.withContext(Dispatchers.Main) {
                        if (muted != null) service.setMuted(muted)
                        else service.toggleMute()
                    }
                }
                "seek" -> {
                    val position = (command["position"] as? Number)?.toLong()
                    if (position != null) {
                        AppLog.d(TAG, "WS seek command: position=$position ms")
                        kotlinx.coroutines.withContext(Dispatchers.Main) { service.seek(position) }
                    } else {
                        AppLog.w(TAG, "WS seek command missing 'position' parameter")
                    }
                }
                "get_state" -> {
                    AppLog.d(TAG, "WS get_state command")
                    // State will be broadcast via broadcastState
                }
                else -> {
                    AppLog.w(TAG, "Unknown WebSocket command: $cmdName")
                }
            }
        } catch (e: Exception) {
            AppLog.e(TAG, "Error processing WebSocket command from client #$connectionId: ${e.message}", e)
        }
    }
}
