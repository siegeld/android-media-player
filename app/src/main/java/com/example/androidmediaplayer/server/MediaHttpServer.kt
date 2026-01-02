package com.example.androidmediaplayer.server

import android.util.Log
import com.example.androidmediaplayer.model.*
import com.example.androidmediaplayer.service.MediaPlayerService
import com.google.gson.Gson
import io.ktor.http.*
import io.ktor.serialization.gson.*
import io.ktor.server.application.*
import io.ktor.server.engine.*
import io.ktor.server.netty.*
import io.ktor.server.plugins.contentnegotiation.*
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
    private val deviceName: String
) {
    companion object {
        private const val TAG = "MediaHttpServer"
    }

    private var server: NettyApplicationEngine? = null
    private val gson = Gson()
    private val wsConnections = ConcurrentHashMap.newKeySet<WebSocketSession>()
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val connectionCounter = AtomicInteger(0)

    fun start() {
        Log.i(TAG, "Starting HTTP/WebSocket server on port $port for device '$deviceName'")
        server = embeddedServer(Netty, port = port) {
            install(ContentNegotiation) {
                gson {
                    setPrettyPrinting()
                }
            }
            install(WebSockets) {
                pingPeriod = Duration.ofSeconds(15)
                timeout = Duration.ofSeconds(30)
                maxFrameSize = Long.MAX_VALUE
                masking = false
            }
            configureRouting()
        }.start(wait = false)
        Log.i(TAG, "Server started successfully on port $port")
    }

    fun stop() {
        Log.i(TAG, "Stopping HTTP server, closing ${wsConnections.size} WebSocket connections")
        scope.cancel()
        wsConnections.forEach { session ->
            scope.launch {
                try {
                    session.close(CloseReason(CloseReason.Codes.GOING_AWAY, "Server shutting down"))
                } catch (e: Exception) {
                    Log.w(TAG, "Error closing WebSocket session: ${e.message}")
                }
            }
        }
        server?.stop(1000, 2000)
        Log.i(TAG, "HTTP server stopped")
    }

    fun broadcastState(state: PlayerState) {
        if (wsConnections.isEmpty()) return

        val json = gson.toJson(state)
        Log.v(TAG, "Broadcasting state to ${wsConnections.size} clients: state=${state.state}")
        wsConnections.forEach { session ->
            scope.launch {
                try {
                    session.send(Frame.Text(json))
                } catch (e: Exception) {
                    Log.w(TAG, "Failed to send to WebSocket client, removing: ${e.message}")
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
                Log.d(TAG, "GET / from $clientIp - returning device info")
                call.respond(mapOf(
                    "name" to deviceName,
                    "type" to "android_media_player",
                    "version" to "1.0",
                    "capabilities" to listOf("play", "pause", "stop", "volume", "seek")
                ))
            }

            // Get current state
            get("/state") {
                val clientIp = call.request.local.remoteHost
                Log.d(TAG, "GET /state from $clientIp")
                call.respond(service.playerState.value)
            }

            // Play media
            post("/play") {
                val clientIp = call.request.local.remoteHost
                try {
                    val contentType = call.request.contentType()
                    if (contentType == ContentType.Application.Json) {
                        val request = call.receive<PlayMediaRequest>()
                        Log.i(TAG, "POST /play from $clientIp - url=${request.url}, title=${request.title}")
                        kotlinx.coroutines.withContext(Dispatchers.Main) {
                            service.playMedia(request.url, request.title, request.artist)
                        }
                        call.respond(ApiResponse(success = true, message = "Playing", state = service.playerState.value))
                    } else {
                        Log.d(TAG, "POST /play from $clientIp - resume playback")
                        kotlinx.coroutines.withContext(Dispatchers.Main) {
                            service.play()
                        }
                        call.respond(ApiResponse(success = true, message = "Resumed", state = service.playerState.value))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "POST /play error from $clientIp: ${e.message}", e)
                    call.respond(HttpStatusCode.BadRequest, ApiResponse(success = false, message = e.message))
                }
            }

            // Pause
            post("/pause") {
                val clientIp = call.request.local.remoteHost
                Log.d(TAG, "POST /pause from $clientIp")
                kotlinx.coroutines.withContext(Dispatchers.Main) {
                    service.pause()
                }
                call.respond(ApiResponse(success = true, message = "Paused", state = service.playerState.value))
            }

            // Stop
            post("/stop") {
                val clientIp = call.request.local.remoteHost
                Log.d(TAG, "POST /stop from $clientIp")
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
                    Log.d(TAG, "POST /volume from $clientIp - level=${request.level}")
                    kotlinx.coroutines.withContext(Dispatchers.Main) {
                        service.setVolume(request.level)
                    }
                    call.respond(ApiResponse(success = true, message = "Volume set", state = service.playerState.value))
                } catch (e: Exception) {
                    Log.e(TAG, "POST /volume error from $clientIp: ${e.message}", e)
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
                        Log.d(TAG, "POST /mute from $clientIp - muted=${request.muted}")
                        kotlinx.coroutines.withContext(Dispatchers.Main) {
                            if (request.muted != null) {
                                service.setMuted(request.muted)
                            } else {
                                service.toggleMute()
                            }
                        }
                    } else {
                        Log.d(TAG, "POST /mute from $clientIp - toggle")
                        kotlinx.coroutines.withContext(Dispatchers.Main) {
                            service.toggleMute()
                        }
                    }
                    call.respond(ApiResponse(success = true, message = "Mute toggled", state = service.playerState.value))
                } catch (e: Exception) {
                    Log.e(TAG, "POST /mute error from $clientIp: ${e.message}", e)
                    call.respond(HttpStatusCode.BadRequest, ApiResponse(success = false, message = e.message))
                }
            }

            // Seek
            post("/seek") {
                val clientIp = call.request.local.remoteHost
                try {
                    val request = call.receive<SeekRequest>()
                    Log.d(TAG, "POST /seek from $clientIp - position=${request.position}ms")
                    kotlinx.coroutines.withContext(Dispatchers.Main) {
                        service.seek(request.position)
                    }
                    call.respond(ApiResponse(success = true, message = "Seeked", state = service.playerState.value))
                } catch (e: Exception) {
                    Log.e(TAG, "POST /seek error from $clientIp: ${e.message}", e)
                    call.respond(HttpStatusCode.BadRequest, ApiResponse(success = false, message = e.message))
                }
            }

            // WebSocket for real-time state updates
            webSocket("/ws") {
                val connectionId = connectionCounter.incrementAndGet()
                val clientInfo = "${call.request.local.remoteHost}:${call.request.local.remotePort}"
                Log.i(TAG, "WebSocket client #$connectionId connected from $clientInfo")
                wsConnections.add(this)

                try {
                    // Send current state on connect
                    val initialState = gson.toJson(service.playerState.value)
                    Log.d(TAG, "Sending initial state to client #$connectionId")
                    send(Frame.Text(initialState))

                    // Keep connection alive and handle incoming messages
                    for (frame in incoming) {
                        when (frame) {
                            is Frame.Text -> {
                                val text = frame.readText()
                                Log.d(TAG, "WebSocket command from client #$connectionId: $text")
                                handleWebSocketCommand(text, connectionId)
                            }
                            is Frame.Ping -> {
                                Log.v(TAG, "Ping from client #$connectionId")
                                send(Frame.Pong(frame.data))
                            }
                            else -> {}
                        }
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "WebSocket error for client #$connectionId: ${e.message}")
                } finally {
                    wsConnections.remove(this)
                    Log.i(TAG, "WebSocket client #$connectionId disconnected, ${wsConnections.size} clients remaining")
                }
            }
        }
    }

    private suspend fun handleWebSocketCommand(text: String, connectionId: Int) {
        try {
            val command = gson.fromJson(text, Map::class.java)
            val cmdName = command["command"]
            Log.d(TAG, "Processing WebSocket command '$cmdName' from client #$connectionId")

            when (cmdName) {
                "play" -> {
                    val url = command["url"] as? String
                    if (url != null) {
                        Log.i(TAG, "WS play command: url=$url, title=${command["title"]}")
                        kotlinx.coroutines.withContext(Dispatchers.Main) {
                            service.playMedia(
                                url,
                                command["title"] as? String,
                                command["artist"] as? String
                            )
                        }
                    } else {
                        Log.d(TAG, "WS play command: resume")
                        kotlinx.coroutines.withContext(Dispatchers.Main) {
                            service.play()
                        }
                    }
                }
                "pause" -> {
                    Log.d(TAG, "WS pause command")
                    kotlinx.coroutines.withContext(Dispatchers.Main) { service.pause() }
                }
                "stop" -> {
                    Log.d(TAG, "WS stop command")
                    kotlinx.coroutines.withContext(Dispatchers.Main) { service.stop() }
                }
                "volume" -> {
                    val level = (command["level"] as? Number)?.toFloat()
                    if (level != null) {
                        Log.d(TAG, "WS volume command: level=$level")
                        kotlinx.coroutines.withContext(Dispatchers.Main) { service.setVolume(level) }
                    } else {
                        Log.w(TAG, "WS volume command missing 'level' parameter")
                    }
                }
                "mute" -> {
                    val muted = command["muted"] as? Boolean
                    Log.d(TAG, "WS mute command: muted=$muted")
                    kotlinx.coroutines.withContext(Dispatchers.Main) {
                        if (muted != null) service.setMuted(muted)
                        else service.toggleMute()
                    }
                }
                "seek" -> {
                    val position = (command["position"] as? Number)?.toLong()
                    if (position != null) {
                        Log.d(TAG, "WS seek command: position=$position ms")
                        kotlinx.coroutines.withContext(Dispatchers.Main) { service.seek(position) }
                    } else {
                        Log.w(TAG, "WS seek command missing 'position' parameter")
                    }
                }
                "get_state" -> {
                    Log.d(TAG, "WS get_state command")
                    // State will be broadcast via broadcastState
                }
                else -> {
                    Log.w(TAG, "Unknown WebSocket command: $cmdName")
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error processing WebSocket command from client #$connectionId: ${e.message}", e)
        }
    }
}
