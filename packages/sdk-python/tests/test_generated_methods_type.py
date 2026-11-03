"""Payloads built by the generated method stubs: each carries its request's
`type`, and leaves off every other field the caller did not set."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any, get_args

import pytest
from pydantic import RootModel

from tetherto.qvac_sdk import methods
from tetherto.qvac_sdk._generated.models import (
    CancelRequestBroad,
    TranscribeRequestAudioChunkFilePath,
)
from tetherto.qvac_sdk.schemas import (
    CancelRequest,
    EmbedRequest,
    TranscribeRequest,
    TranscribeStreamRequest,
)


class Sent(Exception):
    pass


class RecordingTransport:
    """Records the outgoing payload, then stops the call before any response
    is validated."""

    def __init__(self) -> None:
        self.sent: dict[str, Any] | None = None

    async def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sent = payload
        raise Sent

    async def call_stream(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        self.sent = payload
        raise Sent
        yield  # pragma: no cover -- unreachable; makes this an async generator

    async def call_duplex(
        self, payload: dict[str, Any], up: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.sent = payload
        raise Sent
        yield  # pragma: no cover -- unreachable; makes this an async generator


async def no_chunks() -> AsyncIterator[bytes]:
    return
    yield  # pragma: no cover -- unreachable; makes this an async generator


async def send(func: Any, params: Any) -> dict[str, Any]:
    transport = RecordingTransport()
    args: list[Any] = [transport, params]
    if "up" in inspect.signature(func).parameters:
        args.append(no_chunks())
    with pytest.raises(Sent):
        if inspect.isasyncgenfunction(func):
            async for _ in func(*args):
                pass
        else:
            await func(*args)
    assert transport.sent is not None
    return transport.sent


def request_models(cls: Any) -> list[Any]:
    """The concrete models a request class can hold: itself, or every
    variant of a union request, nested unions included."""
    if issubclass(cls, RootModel):
        return [
            model
            for variant in get_args(cls.model_fields["root"].annotation)
            for model in request_models(variant)
        ]
    return [cls]


@pytest.mark.parametrize("name", methods.__all__)
def test_default_type_counts_as_set(name: str):
    func = getattr(methods, name)
    cls = inspect.signature(func, eval_str=True).parameters["params"].annotation
    for model in request_models(cls):
        assert "type" in model.model_construct().model_fields_set, model.__name__


async def test_reply_method_sends_type_and_only_set_fields():
    sent = await send(methods.embed, EmbedRequest(model_id="m", text="hello"))
    assert sent == {"type": "embed", "modelId": "m", "text": "hello"}


async def test_stream_method_sends_type_and_only_set_fields():
    request = TranscribeRequest(
        model_id="m", audio_chunk=TranscribeRequestAudioChunkFilePath(value="/a.wav")
    )
    sent = await send(methods.transcribe, request)
    assert sent == {
        "type": "transcribe",
        "modelId": "m",
        "audioChunk": {"type": "filePath", "value": "/a.wav"},
    }


async def test_duplex_method_sends_type_and_only_set_fields():
    sent = await send(methods.transcribe_stream, TranscribeStreamRequest(model_id="m"))
    assert sent == {"type": "transcribeStream", "modelId": "m"}


async def test_union_request_sends_variant_type_and_only_set_fields():
    request = CancelRequest(root=CancelRequestBroad(operation="broad", model_id="m"))
    sent = await send(methods.cancel, request)
    assert sent == {"type": "cancel", "operation": "broad", "modelId": "m"}
