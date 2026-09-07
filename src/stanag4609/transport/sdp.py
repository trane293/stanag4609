"""SDP description of the ST 0804 MPEG-2 transport-stream RTP profile."""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Final

from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.transport.rtp import (
    RTP_MPEG2_TS_CLOCK_RATE,
    RTP_MPEG2_TS_PAYLOAD_TYPE,
)

DEFAULT_MAX_SDP_SIZE: Final = 65_536
_KNOWN_FIELDS = frozenset("vosiuepcbtrzkam")


def _uint(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _port(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65_535:
        raise ValueError("port must be an integer from 1 to 65535")
    return value


def _literal_address(value: str, *, name: str) -> IPv4Address | IPv6Address:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    try:
        return ip_address(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an IPv4 or IPv6 address") from exc


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if not value or any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{name} must be non-empty and contain no NUL, CR, or LF")
    return value


def _ttl(value: int | None, *, destination: IPv4Address | IPv6Address) -> int | None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255
    ):
        raise ValueError("ttl must be an integer from 0 to 255")
    if isinstance(destination, IPv4Address) and destination.is_multicast:
        if value is None:
            raise ValueError("ttl is required for an IPv4 multicast destination")
    elif value is not None:
        raise ValueError("ttl is only valid for an IPv4 multicast destination")
    return value


@dataclass(frozen=True, slots=True)
class MPEG2TransportSessionDescription:
    """One SDP-described RTP/AVP MPEG-2 transport-stream destination.

    Instances built by an application encode to a small canonical RFC 4566
    description. Instances returned by :func:`parse_mpeg2_transport_sdp`
    retain the complete source, including unrecognized ``a=`` extensions and
    line endings, so an unmodified decode/encode round trip is lossless.
    """

    destination_address: str
    port: int
    origin_address: str
    session_id: int
    session_version: int
    session_name: str = "STANAG 4609 FMV"
    ttl: int | None = None
    _source: bytes | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        destination = _literal_address(
            self.destination_address, name="destination_address"
        )
        origin = _literal_address(self.origin_address, name="origin_address")
        _port(self.port)
        _uint(self.session_id, name="session_id")
        _uint(self.session_version, name="session_version")
        _text(self.session_name, name="session_name")
        _ttl(self.ttl, destination=destination)
        if origin.version not in {4, 6}:  # pragma: no cover - ipaddress invariant
            raise ValueError("origin_address must be IPv4 or IPv6")

    def encode(self) -> bytes:
        """Encode the description, preserving parsed source bytes when available."""

        if self._source is not None:
            return self._source
        destination = ip_address(self.destination_address)
        origin = ip_address(self.origin_address)
        connection = self.destination_address
        if self.ttl is not None:
            connection += f"/{self.ttl}"
        lines = (
            "v=0",
            f"o=- {self.session_id} {self.session_version} IN IP{origin.version} "
            f"{self.origin_address}",
            f"s={self.session_name}",
            f"c=IN IP{destination.version} {connection}",
            "t=0 0",
            f"m=video {self.port} RTP/AVP {RTP_MPEG2_TS_PAYLOAD_TYPE}",
            f"a=rtpmap:{RTP_MPEG2_TS_PAYLOAD_TYPE} "
            f"MP2T/{RTP_MPEG2_TS_CLOCK_RATE}",
        )
        return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _parse_connection(value: str) -> tuple[str, int | None]:
    fields = value.split(" ")
    if len(fields) != 3 or not all(fields):
        raise DecodeError("SDP connection must contain nettype, addrtype, and address")
    nettype, addrtype, encoded_address = fields
    if nettype != "IN" or addrtype not in {"IP4", "IP6"}:
        raise DecodeError("SDP connection must use IN with IP4 or IP6")
    components = encoded_address.split("/")
    if len(components) > 3 or not all(components):
        raise DecodeError("invalid SDP connection address suffix")
    try:
        address = ip_address(components[0])
    except ValueError as exc:
        raise DecodeError("SDP connection must use a literal IP address") from exc
    expected_type = f"IP{address.version}"
    if addrtype != expected_type:
        raise DecodeError(
            f"SDP connection declares {addrtype} but contains {expected_type} address"
        )

    ttl: int | None = None
    if isinstance(address, IPv4Address) and address.is_multicast:
        if len(components) < 2:
            raise DecodeError("IPv4 multicast SDP connection requires a TTL")
        try:
            ttl = int(components[1], 10)
        except ValueError as exc:
            raise DecodeError("IPv4 multicast SDP TTL must be an integer") from exc
        if not 0 <= ttl <= 255:
            raise DecodeError("IPv4 multicast SDP TTL must be from 0 to 255")
    elif isinstance(address, IPv4Address):
        if len(components) != 1:
            raise DecodeError("unicast IPv4 SDP connection cannot include a TTL")
    elif len(components) > 2:
        raise DecodeError("IPv6 SDP connection cannot include a TTL")

    if len(components) == 3:
        try:
            address_count = int(components[2], 10)
        except ValueError as exc:
            raise DecodeError("SDP connection address count must be an integer") from exc
        if address_count != 1:
            raise DecodeError("layered SDP connection addresses are not supported")
    elif isinstance(address, IPv6Address) and len(components) == 2:
        try:
            address_count = int(components[1], 10)
        except ValueError as exc:
            raise DecodeError("SDP connection address count must be an integer") from exc
        if address_count != 1:
            raise DecodeError("layered SDP connection addresses are not supported")
    return str(address), ttl


def _decode_source(
    data: bytes | bytearray | memoryview | str, *, max_size: int
) -> tuple[bytes, list[tuple[str, str]]]:
    if isinstance(max_size, bool) or not isinstance(max_size, int) or max_size < 1:
        raise ValueError("max_size must be a positive integer")
    if isinstance(data, str):
        source = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray, memoryview)):
        source = bytes(data)
    else:
        raise TypeError("SDP data must be bytes-like or str")
    if len(source) > max_size:
        raise LimitExceeded(f"SDP size {len(source)} exceeds configured limit {max_size}")
    if not source:
        raise DecodeError("SDP description is empty")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecodeError("SDP description is not valid UTF-8") from exc
    if "\x00" in text or "\r" in text.replace("\r\n", ""):
        raise DecodeError("SDP description contains prohibited NUL or bare CR")
    parsed: list[tuple[str, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise DecodeError(f"SDP line {line_number} is empty")
        if len(line) < 2 or line[1] != "=" or len(line[0]) != 1:
            raise DecodeError(f"SDP line {line_number} is not type=value")
        if line[0] not in _KNOWN_FIELDS:
            raise DecodeError(f"unknown SDP field {line[0]!r} on line {line_number}")
        parsed.append((line[0], line[2:]))
    return source, parsed


def parse_mpeg2_transport_sdp(
    data: bytes | bytearray | memoryview | str,
    *,
    max_size: int = DEFAULT_MAX_SDP_SIZE,
) -> MPEG2TransportSessionDescription:
    """Parse one RFC 4566 description containing ST 0804 MP2T over RTP/AVP."""

    source, lines = _decode_source(data, max_size=max_size)
    if len(lines) < 3 or [item[0] for item in lines[:3]] != ["v", "o", "s"]:
        raise DecodeError("SDP description must begin with v=, o=, and s= in order")
    if lines[0][1] != "0":
        raise DecodeError(f"unsupported SDP version {lines[0][1]!r}; expected 0")
    origin_fields = lines[1][1].split(" ")
    if len(origin_fields) != 6 or not all(origin_fields):
        raise DecodeError("SDP o= field must contain exactly six values")
    _, session_id_text, session_version_text, nettype, addrtype, origin_address = (
        origin_fields
    )
    if nettype != "IN" or addrtype not in {"IP4", "IP6"}:
        raise DecodeError("SDP origin must use IN with IP4 or IP6")
    try:
        origin = ip_address(origin_address)
    except ValueError as exc:
        raise DecodeError("SDP origin must use a literal IP address") from exc
    if addrtype != f"IP{origin.version}":
        raise DecodeError("SDP origin address does not match its declared address type")
    if not all(
        value.isascii() and value.isdigit()
        for value in (session_id_text, session_version_text)
    ):
        raise DecodeError("SDP session id and version must be unsigned integers")
    session_id = int(session_id_text, 10)
    session_version = int(session_version_text, 10)
    session_name = lines[2][1]
    if not session_name:
        raise DecodeError("SDP session name must not be empty")

    first_media = next((index for index, item in enumerate(lines) if item[0] == "m"), None)
    if first_media is None:
        raise DecodeError("SDP description has no media section")
    if any(sum(kind == required for kind, _ in lines) != 1 for required in "vos"):
        raise DecodeError("SDP description must contain exactly one v=, o=, and s= field")
    invalid_media_field = next(
        (kind for kind, _ in lines[first_media + 1 :] if kind not in "micbka"),
        None,
    )
    if invalid_media_field is not None:
        raise DecodeError(
            f"session-only field {invalid_media_field}= appears after the first media section"
        )
    if not any(kind == "t" for kind, _ in lines[3:first_media]):
        raise DecodeError("SDP session requires timing before its first media section")
    session_connections = [value for kind, value in lines[3:first_media] if kind == "c"]
    if len(session_connections) > 1:
        raise DecodeError("SDP session has more than one connection field")

    media_starts = [index for index, item in enumerate(lines) if item[0] == "m"]
    media_starts.append(len(lines))
    selected: tuple[str, list[tuple[str, str]]] | None = None
    saw_video_rtp = False
    for position, start in enumerate(media_starts[:-1]):
        media = lines[start][1]
        fields = media.split(" ")
        if len(fields) < 4 or not all(fields):
            raise DecodeError("SDP m= field must contain media, port, protocol, and format")
        media_kind, _, protocol, *formats = fields
        if media_kind == "video" and protocol == "RTP/AVP":
            saw_video_rtp = True
        if (
            media_kind == "video"
            and protocol == "RTP/AVP"
            and str(RTP_MPEG2_TS_PAYLOAD_TYPE) in formats
            and selected is None
        ):
            selected = (media, lines[start + 1 : media_starts[position + 1]])
    if selected is None:
        detail = (
            "SDP RTP/AVP video media does not advertise payload type 33"
            if saw_video_rtp
            else "SDP has no RTP/AVP video media for MPEG-2 transport stream"
        )
        raise DecodeError(detail)

    media, media_lines = selected
    _, port_text, _, *_ = media.split(" ")
    if "/" in port_text:
        raise DecodeError("multiple SDP media ports are not supported")
    if not port_text.isascii() or not port_text.isdigit():
        raise DecodeError("SDP RTP port must be an integer from 1 to 65535")
    try:
        port = _port(int(port_text, 10))
    except ValueError as exc:
        raise DecodeError("SDP RTP port must be an integer from 1 to 65535") from exc
    media_connections = [value for kind, value in media_lines if kind == "c"]
    if len(media_connections) > 1:
        raise DecodeError("selected SDP media has more than one connection field")
    connection = media_connections[0] if media_connections else (
        session_connections[0] if session_connections else None
    )
    if connection is None:
        raise DecodeError("selected SDP media has no connection information")
    destination_address, ttl = _parse_connection(connection)

    for kind, value in media_lines:
        if kind != "a" or not value.lower().startswith(
            f"rtpmap:{RTP_MPEG2_TS_PAYLOAD_TYPE} "
        ):
            continue
        _, mapping = value.split(" ", 1)
        if mapping.upper() != f"MP2T/{RTP_MPEG2_TS_CLOCK_RATE}":
            raise DecodeError(
                f"SDP payload type 33 rtpmap must be MP2T/{RTP_MPEG2_TS_CLOCK_RATE}"
            )

    description = MPEG2TransportSessionDescription(
        destination_address=destination_address,
        port=port,
        origin_address=str(origin),
        session_id=session_id,
        session_version=session_version,
        session_name=session_name,
        ttl=ttl,
    )
    object.__setattr__(description, "_source", source)
    return description


__all__ = [
    "DEFAULT_MAX_SDP_SIZE",
    "MPEG2TransportSessionDescription",
    "parse_mpeg2_transport_sdp",
]
