from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from stanag4609.sidecar import (
    Detection,
    FrameEnvelope,
    HTTPInferenceError,
    HTTPJSONAdapter,
    InferenceContext,
    InferenceOutput,
    PixelBoundingBox,
)


@dataclass
class _Response:
    body: bytes
    status: int = 200
    content_length: str | None = None

    def __post_init__(self) -> None:
        self.headers = (
            {} if self.content_length is None else {"Content-Length": self.content_length}
        )

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int) -> bytes:
        return self.body[:amount]


def _context() -> InferenceContext:
    return InferenceContext(FrameEnvelope(7, 90_000, 640, 480, b"pixels"))


def test_http_json_adapter_posts_bounded_json_and_decodes_output() -> None:
    requests: list[tuple[Request, float]] = []

    def open_request(request: Request, *, timeout: float) -> _Response:
        requests.append((request, timeout))
        return _Response(
            json.dumps(
                {"boxes": [[1, 2, 11, 22]], "scores": [0.9], "labels": ["truck"]}
            ).encode(),
            content_length="75",
        )

    def decode(payload: Any, _context: InferenceContext) -> InferenceOutput:
        box = payload["boxes"][0]
        return InferenceOutput(
            (
                Detection(
                    1,
                    PixelBoundingBox(*box),
                    payload["scores"][0],
                    payload["labels"][0],
                ),
            )
        )

    adapter = HTTPJSONAdapter(
        "https://detector.test/v1/infer",
        request_encoder=lambda context: {
            "sequence": context.frame.sequence_number,
            "width": context.frame.width,
        },
        response_decoder=decode,
        headers={"Authorization": "Bearer secret"},
        timeout_seconds=2.5,
        opener=open_request,
    )

    output = asyncio.run(adapter(_context()))

    assert output.detections[0].label == "truck"
    request, timeout = requests[0]
    assert request.method == "POST"
    assert request.full_url == "https://detector.test/v1/infer"
    request_body = request.data
    assert isinstance(request_body, bytes)
    assert json.loads(request_body) == {"sequence": 7, "width": 640}
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("Authorization") == "Bearer secret"
    assert timeout == 2.5


def test_http_json_adapter_roundtrips_through_a_real_local_server() -> None:
    received: list[Any] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            received.append(json.loads(self.rfile.read(length)))
            body = b'{"accepted":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = cast(tuple[str, int], server.server_address)
        adapter = HTTPJSONAdapter(
            f"http://{host}:{port}/infer",
            request_encoder=lambda context: {"pts": context.frame.pts},
            response_decoder=lambda payload, _: InferenceOutput(data=payload),
        )
        output = asyncio.run(adapter(_context()))
        assert output.data == {"accepted": True}
        assert received == [{"pts": 90_000}]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"endpoint": "file:///tmp/infer"}, "HTTP or HTTPS"),
        ({"endpoint": "https://"}, "host"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"max_request_bytes": True}, "max_request_bytes"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
        ({"headers": {"Content-Type": "text/plain"}}, "reserved"),
    ],
)
def test_http_json_adapter_rejects_unsafe_configuration(
    kwargs: dict[str, Any], error: str
) -> None:
    values: dict[str, Any] = {
        "endpoint": "http://localhost:8000/infer",
        "request_encoder": lambda context: {},
        "response_decoder": lambda payload, context: InferenceOutput(),
    }
    values.update(kwargs)
    with pytest.raises((TypeError, ValueError), match=error):
        HTTPJSONAdapter(**values)


def test_http_json_adapter_bounds_requests_and_responses() -> None:
    context = _context()
    too_large_request = HTTPJSONAdapter(
        "http://localhost/infer",
        request_encoder=lambda _: {"pixels": "x" * 100},
        response_decoder=lambda payload, _: InferenceOutput(data=payload),
        max_request_bytes=16,
        opener=lambda request, timeout: _Response(b"{}"),
    )
    with pytest.raises(HTTPInferenceError, match="request exceeds"):
        asyncio.run(too_large_request(context))

    declared_large = HTTPJSONAdapter(
        "http://localhost/infer",
        request_encoder=lambda _: {},
        response_decoder=lambda payload, _: InferenceOutput(data=payload),
        max_response_bytes=4,
        opener=lambda request, timeout: _Response(b"{}", content_length="5"),
    )
    with pytest.raises(HTTPInferenceError, match="Content-Length"):
        asyncio.run(declared_large(context))

    streamed_large = HTTPJSONAdapter(
        "http://localhost/infer",
        request_encoder=lambda _: {},
        response_decoder=lambda payload, _: InferenceOutput(data=payload),
        max_response_bytes=4,
        opener=lambda request, timeout: _Response(b"12345"),
    )
    with pytest.raises(HTTPInferenceError, match="response exceeds"):
        asyncio.run(streamed_large(context))


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (_Response(b"{}", status=503), "status 503"),
        (_Response(b"not-json"), "valid JSON"),
        (_Response(b"{}", content_length="invalid"), "Content-Length"),
    ],
)
def test_http_json_adapter_normalizes_bad_service_responses(
    response: _Response, error: str
) -> None:
    adapter = HTTPJSONAdapter(
        "http://localhost/infer",
        request_encoder=lambda _: {},
        response_decoder=lambda payload, _: InferenceOutput(data=payload),
        opener=lambda request, timeout: response,
    )
    with pytest.raises(HTTPInferenceError, match=error):
        asyncio.run(adapter(_context()))


def test_http_json_adapter_requires_typed_decoder_output_and_context() -> None:
    adapter = HTTPJSONAdapter(
        "http://localhost/infer",
        request_encoder=lambda _: {},
        response_decoder=lambda payload, _: payload,
        opener=lambda request, timeout: _Response(b"{}"),
    )
    with pytest.raises(TypeError, match="InferenceContext"):
        asyncio.run(adapter(object()))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="InferenceOutput"):
        asyncio.run(adapter(_context()))


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (HTTPError("http://localhost", 429, "busy", Message(), None), "status 429"),
        (URLError("offline"), "request failed"),
        (TimeoutError("late"), "request failed"),
    ],
)
def test_http_json_adapter_normalizes_transport_errors(
    failure: Exception, error: str
) -> None:
    def fail(request: Request, *, timeout: float) -> _Response:
        raise failure

    adapter = HTTPJSONAdapter(
        "http://localhost/infer",
        request_encoder=lambda _: {},
        response_decoder=lambda payload, _: InferenceOutput(data=payload),
        opener=fail,
    )
    with pytest.raises(HTTPInferenceError, match=error):
        asyncio.run(adapter(_context()))


def test_http_json_adapter_rejects_non_json_request_values() -> None:
    adapter = HTTPJSONAdapter(
        "http://localhost/infer",
        request_encoder=lambda _: {"score": float("nan")},
        response_decoder=lambda payload, _: InferenceOutput(data=payload),
        opener=lambda request, timeout: _Response(b"{}"),
    )
    with pytest.raises(HTTPInferenceError, match="valid JSON"):
        asyncio.run(adapter(_context()))
