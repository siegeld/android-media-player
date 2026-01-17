package com.example.androidmediaplayer.sendspin

import android.net.Uri
import androidx.media3.common.C
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.TransferListener
import com.example.androidmediaplayer.util.AppLog
import java.io.IOException
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/**
 * Custom ExoPlayer DataSource that reads audio from SendspinAudioBuffer.
 *
 * This DataSource provides a continuous stream of audio data from the buffer,
 * implementing timestamp-based synchronization for precise playback timing.
 */
class SendspinDataSource(
    private val audioBuffer: SendspinAudioBuffer,
    private val clockSync: SendspinClockSync
) : DataSource {

    companion object {
        private const val TAG = "SendspinDataSource"
        private const val URI_SCHEME = "sendspin"
        private const val MAX_WAIT_MS = 100L // Max time to wait for data
        private const val SYNC_THRESHOLD_MICROS = 50_000L // 50ms sync threshold
    }

    private var opened = false
    private var currentChunkData: ByteArray? = null
    private var currentChunkPosition = 0
    private val lock = ReentrantLock()
    private val dataAvailable = lock.newCondition()

    @Volatile
    private var closed = false

    override fun open(dataSpec: DataSpec): Long {
        AppLog.d(TAG, "Opening Sendspin data source")
        opened = true
        closed = false
        currentChunkData = null
        currentChunkPosition = 0
        return C.LENGTH_UNSET.toLong()
    }

    @Throws(IOException::class)
    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (closed) {
            return C.RESULT_END_OF_INPUT
        }

        lock.withLock {
            // If we have leftover data from current chunk, use it
            val chunkData = currentChunkData
            if (chunkData != null && currentChunkPosition < chunkData.size) {
                val remaining = chunkData.size - currentChunkPosition
                val toRead = minOf(remaining, length)
                System.arraycopy(chunkData, currentChunkPosition, buffer, offset, toRead)
                currentChunkPosition += toRead

                if (currentChunkPosition >= chunkData.size) {
                    currentChunkData = null
                    currentChunkPosition = 0
                }
                return toRead
            }

            // Try to get next chunk
            var chunk = audioBuffer.read()

            // Wait briefly for data if buffer is empty
            if (chunk == null) {
                try {
                    dataAvailable.await(MAX_WAIT_MS, TimeUnit.MILLISECONDS)
                    chunk = audioBuffer.read()
                } catch (e: InterruptedException) {
                    Thread.currentThread().interrupt()
                    return 0
                }
            }

            if (chunk == null) {
                // No data available, return 0 to indicate temporary unavailability
                return 0
            }

            // Handle timestamp synchronization
            if (clockSync.isSynced()) {
                val delayMicros = clockSync.delayUntilMicros(chunk.timestampMicros)
                if (delayMicros > SYNC_THRESHOLD_MICROS) {
                    // Wait until it's time to play this chunk
                    val delayMs = delayMicros / 1000
                    if (delayMs > 0 && delayMs < 1000) {
                        try {
                            Thread.sleep(delayMs)
                        } catch (e: InterruptedException) {
                            Thread.currentThread().interrupt()
                        }
                    }
                } else if (delayMicros < -SYNC_THRESHOLD_MICROS) {
                    // Chunk is too old, skip it and try next
                    AppLog.w(TAG, "Skipping late chunk: ${-delayMicros}us behind")
                    return read(buffer, offset, length)
                }
            }

            // Read from this chunk
            val data = chunk.data
            val toRead = minOf(data.size, length)
            System.arraycopy(data, 0, buffer, offset, toRead)

            if (toRead < data.size) {
                // Save remaining data for next read
                currentChunkData = data
                currentChunkPosition = toRead
            }

            return toRead
        }
    }

    override fun getUri(): Uri? {
        return if (opened) Uri.parse("$URI_SCHEME://stream") else null
    }

    override fun close() {
        AppLog.d(TAG, "Closing Sendspin data source")
        closed = true
        opened = false
        lock.withLock {
            currentChunkData = null
            currentChunkPosition = 0
            dataAvailable.signalAll()
        }
    }

    override fun addTransferListener(transferListener: TransferListener) {
        // Not implemented - no transfer tracking needed
    }

    /**
     * Signal that new data is available in the buffer.
     * Called from WebSocket handler when new audio chunk arrives.
     */
    fun notifyDataAvailable() {
        lock.withLock {
            dataAvailable.signalAll()
        }
    }

    /**
     * Factory for creating SendspinDataSource instances.
     */
    class Factory(
        private val audioBuffer: SendspinAudioBuffer,
        private val clockSync: SendspinClockSync
    ) : DataSource.Factory {
        private var dataSource: SendspinDataSource? = null

        override fun createDataSource(): DataSource {
            val source = SendspinDataSource(audioBuffer, clockSync)
            dataSource = source
            return source
        }

        fun getDataSource(): SendspinDataSource? = dataSource

        fun notifyDataAvailable() {
            dataSource?.notifyDataAvailable()
        }
    }
}
