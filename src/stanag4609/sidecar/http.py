"""Dependency-free bounded JSON-over-HTTP inference adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Any, TypeAlias
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from stanag4609.sidecar.model import InferenceContext, InferenceOutput

HTTPJSONRequestEncoder: TypeAlias = Callable[[InferenceContext], Any]
HTTPJSONResponseDecoder: TypeAlias = Callable[[Any, InferenceContext], InferenceOutput]
HTTPOpener: TypeAlias = Callable[..., Any]

_RESERVED_HEADERS = frozenset({"accept", "content-length", "content-type"})


class HTTPInferenceError(RuntimeError):
    """An inference request could not produce a valid bounded JSON response."""


class HTTPJSONAdapter:
    """POST model-specific JSON to a generic HTTP inference service.

    The request and response hooks deliberately own model schemas. Network I/O
    runs in a worker thread so this adapter composes directly with
    :class:`~stanag4609.sidecar.pipeline.InferenceStage` and async graphs without
    adding an HTTP dependency to the core package.
    """

    __slots__ = (
        "endpoint",
        "headers",
        "max_request_bytes",
        "max_response_bytes",
        "opener",
        "request_encoder",
        "response_decoder",
        "timeout_seconds",
    )

    def __init__(
        self,
        endpoint: str,
        *,
        request_encoder: HTTPJSONRequestEncoder,
        response_decoder: HTTPJSONResponseDecoder,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 10.0,
        max_request_bytes: int = 8 * 1024 * 1024,
        max_response_bytes: int = 8 * 1024 * 1024,
        opener: HTTPOpener = urlopen,
    ) -> None:
        if not isinstance(endpoint, str):
            raise TypeError("endpoint must be a string")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("endpoint must use HTTP or HTTPS")
        if not parsed.hostname:
            raise ValueError("endpoint must include a host")
        if not callable(request_encoder):
            raise TypeError("request_encoder must be callable")
        if not callable(response_decoder):
            raise TypeError("response_decoder must be callable")
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be numeric")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        for name, value in (
            ("max_request_bytes", max_request_bytes),
            ("max_response_bytes", max_response_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be positive")
        if headers is not None and not isinstance(headers, Mapping):
            raise TypeError("headers must be a mapping")
        normalized_headers: dict[str, str] = {}
        for header_name, header_value in (headers or {}).items():
            if not isinstance(header_name, str) or not isinstance(header_value, str):
                raise TypeError("HTTP header names and values must be strings")
            if header_name.lower() in _RESERVED_HEADERS:
                raise ValueError(
                    f"HTTP header {header_name!r} is reserved by the JSON adapter"
                )
            normalized_headers[header_name] = header_value
        if not callable(opener):
            raise TypeError("opener must be callable")
        self.endpoint = endpoint
        self.request_encoder = request_encoder
        self.response_decoder = response_decoder
        self.headers = normalized_headers
        self.timeout_seconds = float(timeout_seconds)
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.opener = opener

    async def __call__(self, context: InferenceContext) -> InferenceOutput:
        if not isinstance(context, InferenceContext):
            raise TypeError("context must be InferenceContext")
        try:
            body = json.dumps(
                self.request_encoder(context),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise HTTPInferenceError(
                f"request_encoder did not produce valid JSON: {error}"
            ) from error
        if len(body) > self.max_request_bytes:
            raise HTTPInferenceError(
                f"inference request exceeds {self.max_request_bytes} byte limit"
            )
        request = Request(
            self.endpoint,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **self.headers,
            },
            method="POST",
        )
        response_body = await asyncio.to_thread(self._exchange, request)
        try:
            payload = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HTTPInferenceError("inference service did not return valid JSON") from error
        output = self.response_decoder(payload, context)
        if not isinstance(output, InferenceOutput):
            raise TypeError("response_decoder must return InferenceOutput")
        return output

    def _exchange(self, request: Request) -> bytes:
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", None)
                if status is not None and not 200 <= status < 300:
                    raise HTTPInferenceError(
                        f"inference service returned HTTP status {status}"
                    )
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as error:
                        raise HTTPInferenceError(
                            "inference service returned an invalid Content-Length"
                        ) from error
                    if declared_length < 0 or declared_length > self.max_response_bytes:
                        raise HTTPInferenceError(
                            "inference response Content-Length exceeds configured limit"
                        )
                body = bytes(response.read(self.max_response_bytes + 1))
        except HTTPInferenceError:
            raise
        except HTTPError as error:
            raise HTTPInferenceError(
                f"inference service returned HTTP status {error.code}"
            ) from error
        except (URLError, OSError, TimeoutError) as error:
            raise HTTPInferenceError(f"inference request failed: {error}") from error
        if len(body) > self.max_response_bytes:
            raise HTTPInferenceError(
                f"inference response exceeds {self.max_response_bytes} byte limit"
            )
        return body
