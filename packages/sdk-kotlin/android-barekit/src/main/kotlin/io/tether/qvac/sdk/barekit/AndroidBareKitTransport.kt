package io.tether.qvac.sdk.barekit

import android.content.Context
import io.tether.qvac.sdk.QvacTransport
import io.tether.qvac.sdk.QvacRuntimeProfile
import io.tether.qvac.sdk.QvacWorkerStartException
import io.tether.qvac.sdk.RpcLimits
import io.tether.qvac.sdk.WorkerRpc
import io.tether.qvac.sdk.barekit.worker.start
import io.tether.qvac.sdk.requireSuccessfulWorkerControlResponse
import kotlin.time.Duration.Companion.milliseconds
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import to.holepunch.bare.kit.Worklet
import to.holepunch.bare.stow.host.Ipc

/** The worker started by bare-stow's `bare-kit` harness, with bare-rpc over its `ipc`. */
class AndroidBareKitTransport private constructor(
    private val rpc: WorkerRpc,
    private val ipc: Ipc,
    private val worklet: Worklet,
    override val runtimeProfile: QvacRuntimeProfile,
) : QvacTransport {
    private val closeMutex = Mutex()
    private var closed = false

    override suspend fun call(payload: JsonObject): JsonObject = rpc.call(payload)

    override fun stream(payload: JsonObject): Flow<JsonObject> = rpc.stream(payload)

    override fun duplex(payload: JsonObject, input: Flow<ByteArray>): Flow<JsonObject> = rpc.duplex(payload, input)

    fun pause(lingerMs: Int = DEFAULT_LINGER_MS) {
        worklet.suspend(lingerMs)
    }

    fun resume() {
        worklet.resume()
    }

    override suspend fun close(): Unit = withContext(NonCancellable) {
        closeMutex.withLock {
            if (closed) return@withLock
            closed = true

            runCatching { rpc.close() }
            withTimeoutOrNull(SHUTDOWN_TIMEOUT_MS) {
                runCatching { ipc.terminate() }
            }
            runCatching { worklet.close() }
        }
    }

    companion object {
        suspend fun connect(
            context: Context,
            homeDirectory: String = context.filesDir.absolutePath,
            config: JsonObject = JsonObject(emptyMap()),
            runtimeContext: JsonObject? = null,
            memoryLimitBytes: Int = DEFAULT_MEMORY_LIMIT_BYTES,
            rpcLimits: RpcLimits = RpcLimits(),
        ): AndroidBareKitTransport {
            val started = try {
                start(
                    context,
                    Worklet.Options(memoryLimit = memoryLimitBytes.toLong()),
                    timeout = START_TIMEOUT_MS.milliseconds,
                )
            } catch (error: Throwable) {
                if (error is CancellationException) throw error
                throw QvacWorkerStartException("Failed to start QVAC BareKit worker", error)
            }
            val rpc = WorkerRpc(data = started.ipc.data, send = started.ipc::write, limits = rpcLimits)
            val transport = AndroidBareKitTransport(
                rpc,
                started.ipc,
                started.worklet,
                AndroidRuntimeProfile.load(context),
            )
            try {
                val response = transport.call(
                    buildJsonObject {
                        put("type", "__init_config")
                        if (config.isNotEmpty()) put("config", config)
                        put("runtimeContext", androidRuntimeContext(runtimeContext))
                        put("homeDir", homeDirectory)
                    },
                )
                requireSuccessfulWorkerControlResponse("configuration", response)
                return transport
            } catch (error: Throwable) {
                transport.close()
                if (error is CancellationException) throw error
                throw QvacWorkerStartException("Failed to start QVAC BareKit worker", error)
            }
        }

        // Android hosts report device identity so @qvac/inference can apply its
        // device-specific defaults (e.g. Pixel → llama device=cpu) without the
        // caller configuring anything. Caller-supplied keys override the defaults.
        private fun androidRuntimeContext(override: JsonObject?): JsonObject = buildJsonObject {
            put("platform", "android")
            put("deviceBrand", android.os.Build.MANUFACTURER)
            put("deviceModel", android.os.Build.MODEL)
            override?.forEach { (key, value) -> put(key, value) }
        }

        private const val DEFAULT_MEMORY_LIMIT_BYTES = 0
        private const val DEFAULT_LINGER_MS = 30_000
        private const val SHUTDOWN_TIMEOUT_MS = 10_000L
        private const val START_TIMEOUT_MS = 30_000L
    }
}
