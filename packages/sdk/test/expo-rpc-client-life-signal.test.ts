import { mock, test } from 'node:test'
import assert from 'node:assert/strict'
import { Duplex } from 'node:stream'

class FakeWorklet extends Duplex {
  override _read() {}

  override _write(_chunk: unknown, _encoding: string, callback: () => void) {
    callback()
  }

  async terminate() {
    this.destroy()
  }
}

const started: FakeWorklet[] = []

;(globalThis as { require?: (id: string) => unknown }).require = (id: string) => {
  if (id !== '@qvac/sdk/worker.mobile') throw new Error(`unexpected require: ${id}`)
  return {
    default: {
      async start() {
        const ipc = new FakeWorklet()
        started.push(ipc)
        return { ipc }
      }
    }
  }
}

let initializeGate: Promise<void> = Promise.resolve()
let initializeCalls = 0

mock.module('@/client/init-hooks', {
  namedExports: {
    initializeConfig: async () => {
      initializeCalls++
      await initializeGate
    }
  }
})
mock.module('@/client/config-loader/resolve-config.expo', {
  namedExports: { resolveConfig: async () => undefined }
})
mock.module('@/client/rpc/expo-device-info', {
  namedExports: { getDeviceInfo: async () => ({ platform: 'android' }) }
})
mock.module('@/client/rpc/expo-home-dir', {
  namedExports: { getHomeDir: async () => '/documents' }
})

const { close, getRPC, getWorkerLifeSignal } = await import('@/client/rpc/expo-rpc-client')

test('a worklet that closes unexpectedly aborts the life signal and the next call starts a new one', async () => {
  await getRPC()
  const signal = getWorkerLifeSignal()
  assert.ok(signal)
  assert.equal(signal.aborted, false)

  started[0]!.destroy()
  await new Promise((resolve) => setImmediate(resolve))

  assert.equal(signal.aborted, true)
  assert.equal((signal.reason as { name?: string }).name, 'WORKER_CRASHED')
  assert.equal(getWorkerLifeSignal(), null)

  await getRPC()
  assert.equal(started.length, 2)
  await close()
})

test('calls made while close runs share one new worklet', async () => {
  await getRPC()
  const before = started.length

  const closing = close()
  const [first, second] = await Promise.all([getRPC(), getRPC()])
  await closing

  assert.equal(started.length, before + 1)
  assert.equal(first, second)
  await close()
})

test('a call made while close stops a starting worklet gets a new worklet', async () => {
  let openGate = () => {}
  initializeGate = new Promise((resolve) => {
    openGate = resolve
  })
  const before = started.length
  const calls = initializeCalls
  const starting = getRPC()
  while (initializeCalls === calls) await new Promise((resolve) => setImmediate(resolve))

  const closing = close()
  initializeGate = Promise.resolve()
  const next = getRPC()
  openGate()

  await assert.rejects(starting, (error: { name?: string }) => error.name === 'WORKER_SHUTDOWN')
  await next
  await closing
  assert.equal(started.length, before + 2)
  await close()
})

test('close aborts the life signal as a shutdown', async () => {
  await getRPC()
  const signal = getWorkerLifeSignal()
  assert.ok(signal)

  await close()

  assert.equal(signal.aborted, true)
  assert.equal((signal.reason as { name?: string }).name, 'WORKER_SHUTDOWN')
  assert.equal(getWorkerLifeSignal(), null)
})
