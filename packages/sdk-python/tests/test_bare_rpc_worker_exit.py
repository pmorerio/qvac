"""How BareRpcTransport reports a worker that faults, exits or ignores
`terminate`, over bare-stow's host side and an in-memory fake worker.

Does not need a built worker, so it runs in the fast PR check.
"""

from __future__ import annotations

import asyncio
import functools

import pytest
from conftest import answer_init_config

from tetherto.qvac_sdk.bare_rpc_transport import BareRpcTransport, WorkerError

pytestmark = [pytest.mark.asyncio]


async def test_a_worker_error_after_ready_reaches_the_caller(fake_worker) -> None:
    async def on_request(req) -> None:
        if await answer_init_config(req):
            return
        worker.control("error", message="worker blew up", stack="at x")
        worker.control("exit", code=1)

    worker = fake_worker(on_request)
    transport = BareRpcTransport(worker.start)
    await asyncio.wait_for(transport.connect(), timeout=5)
    try:
        with pytest.raises(WorkerError, match="worker blew up") as raised:
            await asyncio.wait_for(transport.call({"type": "heartbeat"}), timeout=5)
        assert raised.value.stack == "at x"
    finally:
        await transport.close()


async def test_close_terminates_the_worker(fake_worker) -> None:
    worker = fake_worker(answer_init_config)
    transport = BareRpcTransport(worker.start)
    await asyncio.wait_for(transport.connect(), timeout=5)

    await asyncio.wait_for(transport.close(), timeout=5)

    assert worker.terminated.is_set()
    assert transport.ipc is not None
    assert transport.ipc.exit_code == 0


async def test_close_kills_a_worker_that_ignores_terminate(fake_worker) -> None:
    worker = fake_worker(answer_init_config)
    worker.answer_terminate = False
    transport = BareRpcTransport(functools.partial(worker.start, grace=0.2))
    await asyncio.wait_for(transport.connect(), timeout=5)

    await asyncio.wait_for(transport.close(), timeout=5)

    assert worker.terminated.is_set()
    assert worker.closed


async def test_connect_terminates_the_worker_when_init_config_fails(
    fake_worker,
) -> None:
    async def on_request(req) -> None:
        await req.reply(b'{"type":"error","message":"bad config"}')

    worker = fake_worker(on_request)
    transport = BareRpcTransport(worker.start)

    with pytest.raises(Exception, match="bad config"):
        await asyncio.wait_for(transport.connect(), timeout=5)

    assert worker.terminated.is_set()
