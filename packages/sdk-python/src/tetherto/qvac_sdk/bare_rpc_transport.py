r"""Production `tetherto.qvac_sdk._transport.Transport` implementation, backed by
`bare_rpc.RPC` (github.com/holepunchto/bare-rpc-python) over the worker's
bare-stow IPC (github.com/holepunchto/bare-stow-host-python).

The IPC multiplexes the bare-rpc byte stream with bare-stow's control frames:
the worker's `ready`, `error` and `exit`, and the client's `terminate`. A worker
that faults, or exits without saying why, fails the pending calls with
`WorkerError`, which carries the worker's stack or its exit code and signal.

Starting the worker is not this module's job -- `start` is supplied by the
caller (see `tetherto.qvac_sdk.client`).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from typing import Any

import bare_rpc
from bare_stow_host import IPC, WorkerError

from .errors import reconstruct_error

__all__ = ["BareRpcTransport", "WorkerError", "WorkerStart"]

WorkerStart = Callable[[float], Awaitable[IPC]]
"""Starts the worker and returns its IPC once it signals ready within the given
number of seconds."""


def _patch_bare_rpc_outgoing_destroy() -> None:
    """Work around a bare-rpc-python bug: `RPC._on_stream` handles a peer STREAM
    DESTROY frame by calling the *async* `OutgoingStream.destroy()` without
    awaiting or scheduling it, so the coroutine is dropped -- the outgoing stream
    is never closed and Python warns "coroutine 'OutgoingStream.destroy' was
    never awaited" (seen tearing down the completion-orchestrate duplex). Wrap
    `destroy` so it schedules the original on the RPC's own task set -- it
    actually runs and stays referenced -- and returns the task, so a direct
    `await stream.destroy()` keeps working too. Idempotent. Remove once bare-rpc
    schedules/awaits the destroy itself.
    """
    outgoing_stream = bare_rpc.OutgoingStream
    original = outgoing_stream.destroy
    if getattr(original, "_qvac_scheduled", False):
        return

    def destroy(self, error=None):
        task = asyncio.ensure_future(original(self, error))
        self._rpc._tasks.add(task)
        task.add_done_callback(self._rpc._tasks.discard)
        return task

    destroy._qvac_scheduled = True  # type: ignore[attr-defined]
    outgoing_stream.destroy = destroy  # type: ignore[method-assign]


_patch_bare_rpc_outgoing_destroy()


def _json_or_raise(data: bytes) -> Any:
    """Parse a JSON payload; the SDK reports failures in-band as
    {"type": "error", ...} envelopes -- rebuild the typed error (or a generic
    RPCError) so callers get `isinstance`-able classes, not strings."""
    obj = json.loads(data.decode("utf-8"))
    if isinstance(obj, dict) and obj.get("type") == "error":
        raise reconstruct_error(obj)
    return obj


async def _iter_json_lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[Any]:
    """Parse a stream of raw byte chunks into newline-delimited JSON values.

    Scans for newlines from a running offset and compacts the consumed prefix
    once per chunk, so a large frame arriving across many chunks (e.g. a big
    base64 audio buffer on a duplex stream) costs O(bytes), not O(chunks^2)
    from re-splitting a growing buffer on every chunk."""
    buffer = bytearray()
    start = 0
    async for chunk in chunks:
        buffer.extend(chunk)
        while True:
            nl = buffer.find(b"\n", start)
            if nl == -1:
                start = len(buffer)
                break
            line = bytes(buffer[start:nl])
            start = nl + 1
            if line.strip():
                yield _json_or_raise(line)
        if start:
            del buffer[:start]
            start = 0
    if buffer.strip():
        yield _json_or_raise(bytes(buffer))


class BareRpcTransport:
    """Starts a QVAC SDK worker with `start` and speaks to it via
    `bare_rpc.RPC`, satisfying `tetherto.qvac_sdk._transport.Transport`'s async
    call/call_stream/call_duplex shape directly.

    Closing it closes the RPC session, which terminates the worker through its
    IPC: the worker gets bare-stow's grace period to stop, then is killed.
    """

    def __init__(
        self,
        start: WorkerStart,
        *,
        home_dir: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._start = start
        # QVAC_HOME_DIR lets CI/tests pin the worker's storage root (models,
        # registry) to a cacheable, per-OS-explicit path instead of the
        # ambiguous expanduser("~") (which Windows resolves via USERPROFILE).
        self._home_dir = (
            home_dir or os.environ.get("QVAC_HOME_DIR") or os.path.expanduser("~")
        )
        # SDK runtime config (QvacConfig: cacheDirectory, loggerLevel,
        # swarmRelays, plugin/device defaults, ...), sent to the worker as an
        # `__init_config` message on connect -- the same mechanism the JS client
        # uses. QVAC_CACHE_DIR is a convenience env for the common cacheDirectory
        # override (e.g. a shared CI model cache).
        merged_config = dict(config or {})
        cache_dir = os.environ.get("QVAC_CACHE_DIR")
        if cache_dir and "cacheDirectory" not in merged_config:
            merged_config["cacheDirectory"] = cache_dir
        self._config: dict[str, Any] = merged_config
        self.ipc: IPC | None = None
        self.rpc: bare_rpc.RPC | None = None

    async def connect(self, *, timeout: float = 30) -> BareRpcTransport:
        self.ipc = await self._start(timeout)
        self.rpc = bare_rpc.RPC(self.ipc)
        try:
            # Hand over the home folder and SDK config before any method call,
            # mirroring the JS client's `__init_config` on connect.
            init: dict[str, Any] = {"type": "__init_config", "homeDir": self._home_dir}
            if self._config:
                init["config"] = self._config
            _json_or_raise(
                await self.rpc.request(command=0, data=json.dumps(init).encode("utf-8"))
            )
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self.rpc is not None:
            await self.rpc.close()
        elif self.ipc is not None:
            await self.ipc.close()

    async def __aenter__(self) -> BareRpcTransport:
        return await self.connect()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ---- Transport protocol ----------------------------------------------

    def _require_rpc(self) -> bare_rpc.RPC:
        if self.rpc is None:
            raise RuntimeError("BareRpcTransport used before connect()")
        return self.rpc

    async def call(self, payload: dict) -> dict:
        """Unary, via bare_rpc.RPC.request."""
        data = await self._require_rpc().request(
            command=0, data=json.dumps(payload).encode("utf-8")
        )
        return _json_or_raise(data)

    async def call_stream(self, payload: dict) -> AsyncIterator[dict]:
        """Server-stream, via bare_rpc.RPC.request_with_response_stream."""
        stream = await self._require_rpc().request_with_response_stream(
            command=0, data=json.dumps(payload).encode("utf-8")
        )
        async for value in _iter_json_lines(stream):
            yield value

    async def call_duplex(
        self, payload: dict, up: AsyncIterable[bytes]
    ) -> AsyncIterator[dict]:
        """Duplex, via bare_rpc.RPC.create_bidirectional_stream -- first outgoing
        chunk is the JSON payload, then `up`'s chunks; yields parsed response
        chunks with the same buffer-and-split-on-newline handling as call_stream."""
        outgoing, incoming = await self._require_rpc().create_bidirectional_stream(
            command=0
        )
        await outgoing.write(json.dumps(payload).encode("utf-8"))

        async def _pump_up() -> None:
            async for chunk in up:
                await outgoing.write(chunk)
            await outgoing.end()

        pump_task = asyncio.ensure_future(_pump_up())
        try:
            async for value in _iter_json_lines(incoming):
                yield value
        finally:
            if not pump_task.done():
                pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                # Expected: pump_task was cancelled as part of close().
                pass
