package com.example.androidmediaplayer.sendspin

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import com.example.androidmediaplayer.util.AppLog
import kotlinx.coroutines.*
import java.util.concurrent.atomic.AtomicBoolean

/**
 * AudioTrack-based player for Sendspin PCM streams.
 *
 * ExoPlayer requires containerized media (WAV, MP3, etc), so we use AudioTrack
 * directly for headerless raw PCM from Sendspin servers.
 */
class SendspinAudioPlayer(
    private val audioBuffer: SendspinAudioBuffer,
    private val clockSync: SendspinClockSync
) {
    companion object {
        private const val TAG = "SendspinAudioPlayer"
        private const val BUFFER_SIZE_FACTOR = 6 // Multiple of minimum buffer size
        private const val SYNC_THRESHOLD_MICROS = 50_000L // 50ms sync threshold
        private const val PRE_BUFFER_TIMEOUT_MS = 5000L // Max wait for pre-buffering
    }

    private var audioTrack: AudioTrack? = null
    private var playbackJob: Job? = null
    private val isPlaying = AtomicBoolean(false)
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    private var currentSampleRate = 48000
    private var currentChannels = 2
    private var currentBitDepth = 16

    /**
     * Start playback with the given audio format.
     */
    fun start(sampleRate: Int, channels: Int, bitDepth: Int) {
        AppLog.i(TAG, "Starting AudioTrack: ${sampleRate}Hz, ${channels}ch, ${bitDepth}bit")

        currentSampleRate = sampleRate
        currentChannels = channels
        currentBitDepth = bitDepth

        stop() // Stop any existing playback

        try {
            val channelConfig = when (channels) {
                1 -> AudioFormat.CHANNEL_OUT_MONO
                2 -> AudioFormat.CHANNEL_OUT_STEREO
                else -> {
                    AppLog.e(TAG, "Unsupported channel count: $channels")
                    return
                }
            }

            val encoding = when (bitDepth) {
                16 -> AudioFormat.ENCODING_PCM_16BIT
                24 -> AudioFormat.ENCODING_PCM_24BIT_PACKED
                32 -> AudioFormat.ENCODING_PCM_32BIT
                else -> {
                    AppLog.e(TAG, "Unsupported bit depth: $bitDepth")
                    return
                }
            }

            val minBufferSize = AudioTrack.getMinBufferSize(sampleRate, channelConfig, encoding)
            if (minBufferSize <= 0) {
                AppLog.e(TAG, "Invalid min buffer size: $minBufferSize")
                return
            }

            val bufferSize = minBufferSize * BUFFER_SIZE_FACTOR

            audioTrack = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(sampleRate)
                        .setChannelMask(channelConfig)
                        .setEncoding(encoding)
                        .build()
                )
                .setBufferSizeInBytes(bufferSize)
                .setTransferMode(AudioTrack.MODE_STREAM)
                .build()

            // Pre-buffer: wait until we have enough data to fill the AudioTrack buffer
            // This prevents underruns at startup while timestamp sync handles playback timing
            val minPreBuffer = bufferSize
            val startWait = System.currentTimeMillis()
            while (audioBuffer.sizeBytes() < minPreBuffer) {
                if (System.currentTimeMillis() - startWait > PRE_BUFFER_TIMEOUT_MS) {
                    AppLog.w(TAG, "Pre-buffer timeout, starting with ${audioBuffer.sizeBytes()} bytes")
                    break
                }
                Thread.sleep(10)
            }
            AppLog.i(TAG, "Pre-buffered ${audioBuffer.sizeBytes()} bytes (target: $minPreBuffer)")

            audioTrack?.play()
            isPlaying.set(true)
            AppLog.i(TAG, "AudioTrack started with buffer size: $bufferSize")

            // Fast-forward through stale chunks to sync with clock
            if (clockSync.isSynced()) {
                var skippedCount = 0
                while (true) {
                    val chunk = audioBuffer.peek() ?: break
                    val delayMicros = clockSync.delayUntilMicros(chunk.timestampMicros)
                    if (delayMicros >= -500_000) { // Within 500ms - close enough to play
                        break
                    }
                    // Chunk is too old, discard it
                    audioBuffer.read()
                    skippedCount++
                }
                if (skippedCount > 0) {
                    AppLog.i(TAG, "Fast-forwarded past $skippedCount stale chunks to sync with clock")
                }
            }

            // Start playback loop
            playbackJob = scope.launch {
                playbackLoop()
            }
        } catch (e: Exception) {
            AppLog.e(TAG, "Failed to create AudioTrack: ${e.message}", e)
        }
    }

    private suspend fun playbackLoop() {
        AppLog.d(TAG, "Playback loop started")
        var chunkCount = 0
        var lastLogTime = 0L

        // Get AudioTrack latency for sync calculations
        val track = audioTrack
        val trackLatencyMs = if (track != null && android.os.Build.VERSION.SDK_INT >= 19) {
            try {
                // Get playback head position to estimate buffer latency
                val bufferSizeFrames = track.bufferSizeInFrames
                val frameRate = currentSampleRate
                (bufferSizeFrames * 1000L / frameRate)
            } catch (e: Exception) {
                100L // Default assumption: 100ms buffer
            }
        } else {
            100L
        }
        AppLog.i(TAG, "AudioTrack estimated latency: ${trackLatencyMs}ms")

        while (isPlaying.get()) {
            val chunk = audioBuffer.read()

            if (chunk == null) {
                // No data available, wait a bit
                delay(5)
                continue
            }

            chunkCount++

            // Handle timestamp synchronization if clock is synced
            if (clockSync.isSynced()) {
                // Account for AudioTrack buffer latency
                val targetPlayTimeMicros = chunk.timestampMicros - (trackLatencyMs * 1000)
                val delayMicros = clockSync.delayUntilMicros(targetPlayTimeMicros)

                // Log sync info periodically
                val now = System.currentTimeMillis()
                if (now - lastLogTime > 5000) {
                    lastLogTime = now
                    AppLog.d(TAG, "Sync: delay=${delayMicros/1000}ms, offset=${clockSync.getOffsetMicros()/1000}ms, chunks=$chunkCount")
                }

                if (delayMicros > 100_000) { // More than 100ms ahead
                    // Wait until it's closer to play time, but cap the wait
                    val delayMs = minOf(delayMicros / 1000, 500L)
                    if (delayMs > 10) {
                        delay(delayMs - 10) // Leave 10ms margin
                    }
                } else if (delayMicros < -1_000_000) { // More than 1s behind
                    // Chunk is way too old, skip it
                    if (chunkCount % 50 == 0) {
                        AppLog.w(TAG, "Skipping late chunk: ${-delayMicros/1000}ms behind")
                    }
                    continue
                }
                // Between -500ms and +100ms: play it (we're roughly on time)
            }

            // Write audio data to AudioTrack
            if (track != null && track.state == AudioTrack.STATE_INITIALIZED) {
                val written = track.write(chunk.data, 0, chunk.data.size)
                if (written < 0) {
                    AppLog.e(TAG, "AudioTrack write error: $written")
                } else if (chunkCount % 200 == 1) {
                    AppLog.d(TAG, "Playing chunk #$chunkCount: ${chunk.data.size} bytes")
                }
            }
        }

        AppLog.d(TAG, "Playback loop ended, total chunks: $chunkCount")
    }

    /**
     * Stop playback.
     */
    fun stop() {
        AppLog.d(TAG, "Stopping AudioTrack")
        isPlaying.set(false)
        playbackJob?.cancel()
        playbackJob = null

        audioTrack?.let { track ->
            try {
                track.stop()
                track.release()
            } catch (e: Exception) {
                AppLog.e(TAG, "Error stopping AudioTrack: ${e.message}")
            }
        }
        audioTrack = null
    }

    /**
     * Set volume (0.0 to 1.0).
     */
    fun setVolume(volume: Float) {
        val clampedVolume = volume.coerceIn(0f, 1f)
        audioTrack?.setVolume(clampedVolume)
    }

    /**
     * Check if currently playing.
     */
    fun isPlaying(): Boolean = isPlaying.get()

    /**
     * Release resources.
     */
    fun release() {
        stop()
        scope.cancel()
    }
}
