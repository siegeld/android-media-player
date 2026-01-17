package com.example.androidmediaplayer.model

data class PlayerState(
    val state: String = "idle", // idle, playing, paused, buffering
    val volume: Float = 1.0f,
    val muted: Boolean = false,
    val mediaTitle: String? = null,
    val mediaArtist: String? = null,
    val mediaDuration: Long? = null,
    val mediaPosition: Long? = null,
    val mediaUrl: String? = null,
    val error: String? = null // Error message if playback failed
) {
    companion object {
        const val STATE_IDLE = "idle"
        const val STATE_PLAYING = "playing"
        const val STATE_PAUSED = "paused"
        const val STATE_BUFFERING = "buffering"
    }
}

data class PlayMediaRequest(
    val url: String,
    val title: String? = null,
    val artist: String? = null
)

data class VolumeRequest(
    val level: Float
)

data class MuteRequest(
    val muted: Boolean? = null // null means toggle
)

data class SeekRequest(
    val position: Long
)

data class NameRequest(
    val name: String
)

data class ApiResponse(
    val success: Boolean,
    val message: String? = null,
    val state: PlayerState? = null
)
