package com.example.androidmediaplayer.sendspin

/**
 * Connection state for Sendspin client.
 */
enum class SendspinConnectionState {
    DISCONNECTED,
    CONNECTING,
    HANDSHAKING,
    SYNCING_CLOCK,
    CONNECTED,
    STREAMING,
    ERROR
}

/**
 * Current state of the Sendspin player.
 */
data class SendspinPlayerState(
    val connectionState: SendspinConnectionState = SendspinConnectionState.DISCONNECTED,
    val serverId: String? = null,
    val serverName: String? = null,
    val clockOffsetMicros: Long? = null,
    val clockSynced: Boolean = false,
    val streamActive: Boolean = false,
    val currentCodec: String? = null,
    val sampleRate: Int? = null,
    val channels: Int? = null,
    val volume: Int = 100,
    val muted: Boolean = false,
    val bufferHealthPercent: Float = 0f,
    val errorMessage: String? = null
)

/**
 * Audio stream configuration received from server.
 */
data class SendspinStreamConfig(
    val codec: String,
    val sampleRate: Int,
    val channels: Int,
    val bitDepth: Int,
    val codecHeader: ByteArray? = null
) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is SendspinStreamConfig) return false
        return codec == other.codec &&
                sampleRate == other.sampleRate &&
                channels == other.channels &&
                bitDepth == other.bitDepth &&
                codecHeader.contentEquals(other.codecHeader)
    }

    override fun hashCode(): Int {
        var result = codec.hashCode()
        result = 31 * result + sampleRate
        result = 31 * result + channels
        result = 31 * result + bitDepth
        result = 31 * result + (codecHeader?.contentHashCode() ?: 0)
        return result
    }
}
