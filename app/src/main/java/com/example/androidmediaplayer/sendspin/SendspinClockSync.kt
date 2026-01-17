package com.example.androidmediaplayer.sendspin

import java.util.concurrent.atomic.AtomicLong

/**
 * Clock synchronization for Sendspin protocol.
 * Maintains offset between local clock and server clock with microsecond precision.
 *
 * Uses NTP-like algorithm:
 * - Client sends timestamp when request transmitted
 * - Server responds with client timestamp + server received + server transmitted
 * - Client computes round-trip time and estimates clock offset
 */
class SendspinClockSync {
    companion object {
        private const val MAX_SAMPLES = 10
        private const val MIN_SAMPLES_FOR_SYNC = 3
    }

    // Clock offset: serverTime = localTime + offset
    private val clockOffsetMicros = AtomicLong(0)
    private val syncSamples = mutableListOf<Long>()
    private var synced = false

    /**
     * Get current local time in microseconds.
     */
    fun localTimeMicros(): Long = System.nanoTime() / 1000

    /**
     * Process server time response and update clock offset.
     *
     * @param clientTransmitted Time when client sent the request (microseconds)
     * @param serverReceived Time when server received the request (server clock, microseconds)
     * @param serverTransmitted Time when server sent the response (server clock, microseconds)
     * @return Updated clock offset in microseconds
     */
    fun onTimeResponse(
        clientTransmitted: Long,
        serverReceived: Long,
        serverTransmitted: Long
    ): Long {
        val clientReceived = localTimeMicros()

        // Calculate round-trip time
        val roundTrip = (clientReceived - clientTransmitted) - (serverTransmitted - serverReceived)

        // Estimate one-way delay (assume symmetric)
        val oneWayDelay = roundTrip / 2

        // Estimate server time at the moment we received the response
        val estimatedServerNow = serverTransmitted + oneWayDelay

        // Calculate offset: serverTime = localTime + offset
        val offset = estimatedServerNow - clientReceived

        synchronized(syncSamples) {
            syncSamples.add(offset)
            if (syncSamples.size > MAX_SAMPLES) {
                syncSamples.removeAt(0)
            }

            // Use median for stability (resistant to outliers)
            val sortedSamples = syncSamples.sorted()
            val medianOffset = sortedSamples[sortedSamples.size / 2]
            clockOffsetMicros.set(medianOffset)

            synced = syncSamples.size >= MIN_SAMPLES_FOR_SYNC
        }

        return clockOffsetMicros.get()
    }

    /**
     * Convert server timestamp to local timestamp.
     */
    fun serverToLocalMicros(serverMicros: Long): Long {
        return serverMicros - clockOffsetMicros.get()
    }

    /**
     * Convert local timestamp to server timestamp.
     */
    fun localToServerMicros(localMicros: Long): Long {
        return localMicros + clockOffsetMicros.get()
    }

    /**
     * Get current clock offset in microseconds.
     */
    fun getOffsetMicros(): Long = clockOffsetMicros.get()

    /**
     * Check if clock is synchronized (has enough samples).
     */
    fun isSynced(): Boolean = synced

    /**
     * Get number of sync samples collected.
     */
    fun getSampleCount(): Int = synchronized(syncSamples) { syncSamples.size }

    /**
     * Reset clock synchronization state.
     */
    fun reset() {
        synchronized(syncSamples) {
            syncSamples.clear()
        }
        clockOffsetMicros.set(0)
        synced = false
    }

    /**
     * Calculate delay until a server timestamp should be played locally.
     *
     * @param serverTimeMicros Timestamp from server
     * @return Delay in microseconds (negative if timestamp is in the past)
     */
    fun delayUntilMicros(serverTimeMicros: Long): Long {
        val localPlayTime = serverToLocalMicros(serverTimeMicros)
        return localPlayTime - localTimeMicros()
    }
}
