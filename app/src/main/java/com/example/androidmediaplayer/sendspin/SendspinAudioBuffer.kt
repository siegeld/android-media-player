package com.example.androidmediaplayer.sendspin

import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

/**
 * Thread-safe ring buffer for timestamped audio chunks from Sendspin server.
 *
 * WebSocket handler writes audio chunks with timestamps.
 * ExoPlayer DataSource reads chunks and schedules playback based on timestamps.
 */
class SendspinAudioBuffer(
    private val capacityBytes: Int = DEFAULT_CAPACITY
) {
    companion object {
        const val DEFAULT_CAPACITY = 4 * 1024 * 1024 // 4MB
    }

    /**
     * Timestamped audio chunk.
     */
    data class Chunk(
        val timestampMicros: Long,
        val data: ByteArray
    ) {
        override fun equals(other: Any?): Boolean {
            if (this === other) return true
            if (other !is Chunk) return false
            return timestampMicros == other.timestampMicros && data.contentEquals(other.data)
        }

        override fun hashCode(): Int {
            return 31 * timestampMicros.hashCode() + data.contentHashCode()
        }
    }

    private val chunks = ConcurrentLinkedQueue<Chunk>()
    private val totalBytes = AtomicInteger(0)
    private val chunksWritten = AtomicLong(0)
    private val chunksRead = AtomicLong(0)

    /**
     * Write an audio chunk to the buffer.
     *
     * @param chunk The timestamped audio chunk
     * @return true if written successfully, false if buffer is full
     */
    fun write(chunk: Chunk): Boolean {
        if (totalBytes.get() + chunk.data.size > capacityBytes) {
            return false // Buffer full
        }
        chunks.offer(chunk)
        totalBytes.addAndGet(chunk.data.size)
        chunksWritten.incrementAndGet()
        return true
    }

    /**
     * Write raw audio data with timestamp.
     */
    fun write(timestampMicros: Long, data: ByteArray): Boolean {
        return write(Chunk(timestampMicros, data))
    }

    /**
     * Read the next audio chunk from the buffer.
     *
     * @return The next chunk, or null if buffer is empty
     */
    fun read(): Chunk? {
        val chunk = chunks.poll()
        if (chunk != null) {
            totalBytes.addAndGet(-chunk.data.size)
            chunksRead.incrementAndGet()
        }
        return chunk
    }

    /**
     * Peek at the next chunk without removing it.
     */
    fun peek(): Chunk? = chunks.peek()

    /**
     * Clear all chunks from the buffer.
     */
    fun clear() {
        chunks.clear()
        totalBytes.set(0)
    }

    /**
     * Get current buffer size in bytes.
     */
    fun sizeBytes(): Int = totalBytes.get()

    /**
     * Get current buffer usage as a percentage.
     */
    fun usagePercent(): Float = (totalBytes.get().toFloat() / capacityBytes) * 100f

    /**
     * Get buffer health as percentage (alias for usagePercent).
     */
    fun healthPercent(): Float = usagePercent()

    /**
     * Check if buffer is empty.
     */
    fun isEmpty(): Boolean = chunks.isEmpty()

    /**
     * Check if buffer has data.
     */
    fun hasData(): Boolean = !isEmpty()

    /**
     * Get number of chunks currently in buffer.
     */
    fun chunkCount(): Int = chunks.size

    /**
     * Get total chunks written since creation/reset.
     */
    fun totalChunksWritten(): Long = chunksWritten.get()

    /**
     * Get total chunks read since creation/reset.
     */
    fun totalChunksRead(): Long = chunksRead.get()

    /**
     * Get buffer capacity in bytes.
     */
    fun capacity(): Int = capacityBytes

    /**
     * Get available space in bytes.
     */
    fun availableBytes(): Int = capacityBytes - totalBytes.get()

    /**
     * Reset statistics counters.
     */
    fun resetStats() {
        chunksWritten.set(0)
        chunksRead.set(0)
    }
}
