package com.example.androidmediaplayer.sendspin

import com.google.gson.Gson
import com.google.gson.JsonObject
import com.google.gson.annotations.SerializedName
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Sendspin protocol message types and serialization.
 * See: https://www.sendspin-audio.com/spec/
 */
object SendspinProtocol {
    const val VERSION = 1
    const val DEFAULT_PORT = 8927
    const val SERVICE_TYPE = "_sendspin._tcp."

    private val gson = Gson()

    // Binary message type prefixes
    object BinaryType {
        const val AUDIO: Byte = 4
        const val ARTWORK_CHANNEL_0: Byte = 8
        const val ARTWORK_CHANNEL_1: Byte = 9
        const val ARTWORK_CHANNEL_2: Byte = 10
        const val ARTWORK_CHANNEL_3: Byte = 11
        const val VISUALIZER: Byte = 16
    }

    // Codec identifiers
    object Codec {
        const val OPUS = "opus"
        const val FLAC = "flac"
        const val PCM = "pcm"
    }

    // Player states
    object PlayerState {
        const val SYNCHRONIZED = "synchronized"
        const val ERROR = "error"
    }

    // Message type strings
    object MessageType {
        const val CLIENT_HELLO = "client/hello"
        const val SERVER_HELLO = "server/hello"
        const val CLIENT_TIME = "client/time"
        const val SERVER_TIME = "server/time"
        const val CLIENT_STATE = "client/state"
        const val SERVER_STATE = "server/state"
        const val SERVER_COMMAND = "server/command"
        const val STREAM_START = "stream/start"
        const val STREAM_CLEAR = "stream/clear"
        const val STREAM_END = "stream/end"
        const val STREAM_REQUEST_FORMAT = "stream/request-format"
    }

    // ============= Data Classes for Messages =============

    data class Message(
        val type: String,
        val payload: JsonObject
    )

    // Device info for hello message
    data class DeviceInfo(
        @SerializedName("product_name") val productName: String?,
        val manufacturer: String?,
        @SerializedName("software_version") val softwareVersion: String?
    )

    // Audio format specification
    data class AudioFormat(
        val codec: String,
        val channels: Int,
        @SerializedName("sample_rate") val sampleRate: Int,
        @SerializedName("bit_depth") val bitDepth: Int
    )

    // Player support declaration in hello
    data class PlayerSupport(
        @SerializedName("supported_formats") val supportedFormats: List<AudioFormat>,
        @SerializedName("buffer_capacity") val bufferCapacity: Int,
        @SerializedName("supported_commands") val supportedCommands: List<String>
    )

    // Client hello payload
    data class ClientHelloPayload(
        @SerializedName("client_id") val clientId: String,
        val name: String,
        @SerializedName("device_info") val deviceInfo: DeviceInfo?,
        val version: Int,
        @SerializedName("supported_roles") val supportedRoles: List<String>,
        @SerializedName("player_support") val playerSupport: PlayerSupport?
    )

    // Server hello payload
    data class ServerHelloPayload(
        @SerializedName("server_id") val serverId: String,
        val name: String,
        val version: Int,
        @SerializedName("active_roles") val activeRoles: List<String>,
        @SerializedName("connection_reason") val connectionReason: String?
    )

    // Time sync payloads
    data class ClientTimePayload(
        @SerializedName("client_transmitted") val clientTransmitted: Long
    )

    data class ServerTimePayload(
        @SerializedName("client_transmitted") val clientTransmitted: Long,
        @SerializedName("server_received") val serverReceived: Long,
        @SerializedName("server_transmitted") val serverTransmitted: Long
    )

    // Stream start player config
    data class StreamPlayerConfig(
        val codec: String,
        @SerializedName("sample_rate") val sampleRate: Int,
        val channels: Int,
        @SerializedName("bit_depth") val bitDepth: Int,
        @SerializedName("codec_header") val codecHeader: String?
    )

    data class StreamStartPayload(
        val player: StreamPlayerConfig?
    )

    // Player state for client/state
    data class PlayerStatePayload(
        val state: String,
        val volume: Int,
        val muted: Boolean
    )

    data class ClientStatePayload(
        val player: PlayerStatePayload?
    )

    // Player command from server
    data class PlayerCommandPayload(
        val command: String,
        val volume: Int?,
        val mute: Boolean?
    )

    data class ServerCommandPayload(
        val player: PlayerCommandPayload?
    )

    // Stream clear/end
    data class StreamClearPayload(
        val roles: List<String>
    )

    // ============= Message Creation =============

    fun createClientHello(
        clientId: String,
        name: String,
        deviceInfo: DeviceInfo?,
        bufferCapacity: Int = 4 * 1024 * 1024
    ): String {
        val payload = ClientHelloPayload(
            clientId = clientId,
            name = name,
            deviceInfo = deviceInfo,
            version = VERSION,
            supportedRoles = listOf("player@v1"),
            playerSupport = PlayerSupport(
                // Only PCM for now - ExoPlayer needs special handling for raw Opus/FLAC frames
                supportedFormats = listOf(
                    AudioFormat(Codec.PCM, 2, 48000, 16),
                    AudioFormat(Codec.PCM, 2, 44100, 16)
                ),
                bufferCapacity = bufferCapacity,
                supportedCommands = listOf("volume", "mute")
            )
        )
        val message = mapOf(
            "type" to MessageType.CLIENT_HELLO,
            "payload" to payload
        )
        return gson.toJson(message)
    }

    fun createClientTime(clientTransmittedMicros: Long): String {
        val message = mapOf(
            "type" to MessageType.CLIENT_TIME,
            "payload" to ClientTimePayload(clientTransmittedMicros)
        )
        return gson.toJson(message)
    }

    fun createClientState(state: String, volume: Int, muted: Boolean): String {
        val message = mapOf(
            "type" to MessageType.CLIENT_STATE,
            "payload" to ClientStatePayload(
                player = PlayerStatePayload(state, volume, muted)
            )
        )
        return gson.toJson(message)
    }

    // ============= Message Parsing =============

    fun parseMessage(json: String): Message? {
        return try {
            gson.fromJson(json, Message::class.java)
        } catch (e: Exception) {
            null
        }
    }

    fun parseServerHello(payload: JsonObject): ServerHelloPayload? {
        return try {
            gson.fromJson(payload, ServerHelloPayload::class.java)
        } catch (e: Exception) {
            null
        }
    }

    fun parseServerTime(payload: JsonObject): ServerTimePayload? {
        return try {
            gson.fromJson(payload, ServerTimePayload::class.java)
        } catch (e: Exception) {
            null
        }
    }

    fun parseStreamStart(payload: JsonObject): StreamStartPayload? {
        return try {
            gson.fromJson(payload, StreamStartPayload::class.java)
        } catch (e: Exception) {
            null
        }
    }

    fun parseServerCommand(payload: JsonObject): ServerCommandPayload? {
        return try {
            gson.fromJson(payload, ServerCommandPayload::class.java)
        } catch (e: Exception) {
            null
        }
    }

    fun parseStreamClear(payload: JsonObject): StreamClearPayload? {
        return try {
            gson.fromJson(payload, StreamClearPayload::class.java)
        } catch (e: Exception) {
            null
        }
    }

    // ============= Binary Message Parsing =============

    data class AudioChunk(
        val timestampMicros: Long,
        val audioData: ByteArray
    ) {
        override fun equals(other: Any?): Boolean {
            if (this === other) return true
            if (other !is AudioChunk) return false
            return timestampMicros == other.timestampMicros && audioData.contentEquals(other.audioData)
        }
        override fun hashCode(): Int {
            return 31 * timestampMicros.hashCode() + audioData.contentHashCode()
        }
    }

    fun parseBinaryAudio(data: ByteArray): AudioChunk? {
        if (data.size < 9) return null
        if (data[0] != BinaryType.AUDIO) return null

        val buffer = ByteBuffer.wrap(data, 1, 8).order(ByteOrder.BIG_ENDIAN)
        val timestamp = buffer.long
        val audioData = data.copyOfRange(9, data.size)

        return AudioChunk(timestamp, audioData)
    }
}
