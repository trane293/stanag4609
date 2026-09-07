from __future__ import annotations

import pytest

from stanag4609 import (
    MPEG2TransportSessionDescription as PublicMPEG2TransportSessionDescription,
)
from stanag4609.errors import DecodeError, LimitExceeded
from stanag4609.transport.sdp import (
    DEFAULT_MAX_SDP_SIZE,
    MPEG2TransportSessionDescription,
    parse_mpeg2_transport_sdp,
)


def _sdp(
    *,
    origin: bytes = b"- 1 1 IN IP4 192.0.2.1",
    session_lines: bytes = b"t=0 0\n",
    media: bytes = b"video 5004 RTP/AVP 33",
    media_lines: bytes = b"c=IN IP4 239.1.1.1/1\n",
) -> bytes:
    return (
        b"v=0\n"
        + b"o="
        + origin
        + b"\ns=mission\n"
        + session_lines
        + b"m="
        + media
        + b"\n"
        + media_lines
    )


def test_mpeg2_transport_sdp_encodes_st0804_rtp_avp_profile() -> None:
    description = MPEG2TransportSessionDescription(
        destination_address="239.10.20.30",
        port=5004,
        origin_address="192.0.2.10",
        session_id=3_978_739_200,
        session_version=3_978_739_201,
        session_name="STANAG 4609 mission",
        ttl=16,
    )

    encoded = description.encode()

    assert encoded == (
        b"v=0\r\n"
        b"o=- 3978739200 3978739201 IN IP4 192.0.2.10\r\n"
        b"s=STANAG 4609 mission\r\n"
        b"c=IN IP4 239.10.20.30/16\r\n"
        b"t=0 0\r\n"
        b"m=video 5004 RTP/AVP 33\r\n"
        b"a=rtpmap:33 MP2T/90000\r\n"
    )
    assert parse_mpeg2_transport_sdp(encoded) == description
    assert PublicMPEG2TransportSessionDescription is MPEG2TransportSessionDescription


def test_sdp_parser_accepts_lf_media_connection_and_preserves_source() -> None:
    source = (
        b"v=0\n"
        b"o=relay 100 101 IN IP6 2001:db8::1\n"
        b"s=FMV relay\n"
        b"t=0 0\n"
        b"a=tool:receiver-test\n"
        b"m=audio 5002 RTP/AVP 14\n"
        b"c=IN IP6 2001:db8::2\n"
        b"m=video 5004 RTP/AVP 33\n"
        b"i=Multiplexed imagery, metadata, and audio\n"
        b"c=IN IP6 ff15::1234\n"
        b"a=recvonly\n"
        b"a=x-vendor-extension:retained\n"
        b"a=rtpmap:33 MP2T/90000\n"
    )

    description = parse_mpeg2_transport_sdp(source)

    assert description.destination_address == "ff15::1234"
    assert description.port == 5004
    assert description.origin_address == "2001:db8::1"
    assert description.session_id == 100
    assert description.session_version == 101
    assert description.session_name == "FMV relay"
    assert description.ttl is None
    assert description.encode() == source


def test_sdp_encoder_supports_ipv6_unicast_and_unicode_session_name() -> None:
    description = MPEG2TransportSessionDescription(
        destination_address="2001:db8::20",
        port=5004,
        origin_address="2001:db8::10",
        session_id=1,
        session_version=2,
        session_name="Arctic reconnaissance",
    )

    assert b"o=- 1 2 IN IP6 2001:db8::10\r\n" in description.encode()
    assert b"c=IN IP6 2001:db8::20\r\n" in description.encode()
    assert parse_mpeg2_transport_sdp(description.encode()) == description


@pytest.mark.parametrize("container", [bytearray, memoryview])
def test_sdp_parser_accepts_all_documented_bytes_like_inputs(
    container: type[bytearray] | type[memoryview],
) -> None:
    assert parse_mpeg2_transport_sdp(container(_sdp())).port == 5004


def test_sdp_parser_accepts_text_and_default_size_bound() -> None:
    assert DEFAULT_MAX_SDP_SIZE == 65_536
    assert parse_mpeg2_transport_sdp(_sdp().decode()).destination_address == "239.1.1.1"


@pytest.mark.parametrize(
    ("source", "match"),
    [
        (b"", "empty"),
        (b"v=1\no=- 1 1 IN IP4 192.0.2.1\ns=x\nt=0 0\n", "version"),
        (b"v=0\ns=x\no=- 1 1 IN IP4 192.0.2.1\nt=0 0\n", "v=, o=, and s="),
        (
            b"v=0\no=- +1 1 IN IP4 192.0.2.1\ns=x\nt=0 0\n"
            b"m=video 5004 RTP/AVP 33\nc=IN IP4 239.1.1.1/1\n",
            "unsigned integers",
        ),
        (
            b"v=0\no=- 1 1 IN IP4 192.0.2.1\ns=x\nv=0\nt=0 0\n"
            b"m=video 5004 RTP/AVP 33\nc=IN IP4 239.1.1.1/1\n",
            "exactly one v=, o=, and s=",
        ),
        (
            b"v=0\no=- 1 1 IN IP4 192.0.2.1\ns=x\nt=0 0\n"
            b"m=video 5004 RTP/AVP 33\nc=IN IP4 239.1.1.1/1\nt=1 2\n",
            "session-only field",
        ),
        (
            b"v=0\no=- 1 1 IN IP4 192.0.2.1\ns=x\n"
            b"m=video 5004 RTP/AVP 33\nc=IN IP4 239.1.1.1/1\nt=0 0\n",
            "session-only field",
        ),
        (
            b"v=0\no=- 1 1 IN IP4 192.0.2.1\ns=x\nt=0 0\n"
            b"m=video 5004 RTP/AVP 96\nc=IN IP4 239.1.1.1/1\n",
            "payload type 33",
        ),
        (
            b"v=0\no=- 1 1 IN IP4 192.0.2.1\ns=x\nt=0 0\n"
            b"m=video 5004 RTP/AVP 33\nc=IN IP4 239.1.1.1\n",
            "TTL",
        ),
        (
            b"v=0\no=- 1 1 IN IP4 192.0.2.1\ns=x\nt=0 0\n"
            b"m=video 5004 RTP/AVP 33\nc=IN IP4 239.1.1.1/1\n"
            b"a=rtpmap:33 MP2T/8000\n",
            "MP2T/90000",
        ),
        (
            b"v=0\no=- 1 1 IN IP4 192.0.2.1\ns=x\nt=0 0\n"
            b"m=video 5004 RTP/AVP 33\nc=IN IP4 239.1.1.1/1\nq=unknown\n",
            "unknown SDP field",
        ),
    ],
)
def test_sdp_parser_rejects_invalid_or_non_mp2t_descriptions(
    source: bytes, match: str
) -> None:
    with pytest.raises(DecodeError, match=match):
        parse_mpeg2_transport_sdp(source)


def test_sdp_parser_is_type_and_size_bounded() -> None:
    with pytest.raises(TypeError, match="bytes-like or str"):
        parse_mpeg2_transport_sdp(123)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_size"):
        parse_mpeg2_transport_sdp(b"x", max_size=0)
    with pytest.raises(LimitExceeded, match="exceeds"):
        parse_mpeg2_transport_sdp(b"v=0\n" + b"x" * 20, max_size=8)
    with pytest.raises(DecodeError, match="UTF-8"):
        parse_mpeg2_transport_sdp(b"\xff")


@pytest.mark.parametrize(
    ("source", "match"),
    [
        (_sdp(session_lines=b"t=0 0\n\n"), "line 5 is empty"),
        (_sdp(session_lines=b"not-a-field\n"), "type=value"),
        (_sdp(origin=b"- 1 1 IN IP4"), "exactly six"),
        (_sdp(origin=b"- 1 1 XX IP4 192.0.2.1"), "origin must use IN"),
        (_sdp(origin=b"- 1 1 IN IP4 example.test"), "literal IP"),
        (_sdp(origin=b"- 1 1 IN IP6 192.0.2.1"), "does not match"),
        (
            b"v=0\no=- 1 1 IN IP4 192.0.2.1\ns=\nt=0 0\n"
            b"m=video 5004 RTP/AVP 33\nc=IN IP4 239.1.1.1/1\n",
            "name must not be empty",
        ),
        (b"v=0\no=- 1 1 IN IP4 192.0.2.1\ns=x\nt=0 0\n", "no media"),
        (
            _sdp(
                session_lines=(
                    b"c=IN IP4 192.0.2.20\nc=IN IP4 192.0.2.21\nt=0 0\n"
                )
            ),
            "more than one connection",
        ),
        (_sdp(media=b"video 5004 RTP/AVP"), "m= field"),
        (_sdp(media=b"application 5004 UDP 33"), "no RTP/AVP video"),
        (_sdp(media=b"video 5004/2 RTP/AVP 33"), "multiple SDP media ports"),
        (_sdp(media=b"video plus RTP/AVP 33"), "RTP port"),
        (_sdp(media=b"video 70000 RTP/AVP 33"), "RTP port"),
        (
            _sdp(
                media_lines=(
                    b"c=IN IP4 239.1.1.1/1\nc=IN IP4 239.1.1.2/1\n"
                )
            ),
            "selected SDP media has more than one connection",
        ),
        (_sdp(media_lines=b"a=recvonly\n"), "no connection information"),
        (_sdp(media_lines=b"c=XX IP4 239.1.1.1/1\n"), "must use IN"),
        (_sdp(media_lines=b"c=IN IP4\n"), "nettype, addrtype"),
        (_sdp(media_lines=b"c=IN IP4 239.1.1.1//1\n"), "invalid.*suffix"),
        (_sdp(media_lines=b"c=IN IP4 example.test/1\n"), "literal IP"),
        (_sdp(media_lines=b"c=IN IP6 239.1.1.1/1\n"), "declares IP6"),
        (_sdp(media_lines=b"c=IN IP4 239.1.1.1/nope\n"), "TTL must be"),
        (_sdp(media_lines=b"c=IN IP4 239.1.1.1/256\n"), "TTL must be from"),
        (_sdp(media_lines=b"c=IN IP4 192.0.2.20/1\n"), "unicast IPv4"),
        (_sdp(media_lines=b"c=IN IP6 ff15::1/1/2\n"), "cannot include a TTL"),
        (_sdp(media_lines=b"c=IN IP4 239.1.1.1/1/nope\n"), "count must be"),
        (_sdp(media_lines=b"c=IN IP4 239.1.1.1/1/2\n"), "layered"),
        (_sdp(media_lines=b"c=IN IP6 ff15::1/nope\n"), "count must be"),
        (_sdp(media_lines=b"c=IN IP6 ff15::1/2\n"), "layered"),
        (_sdp().replace(b"\n", b"\r", 1), "bare CR"),
        (_sdp() + b"\x00", "NUL"),
    ],
)
def test_sdp_parser_rejects_malformed_session_and_connection_fields(
    source: bytes, match: str
) -> None:
    with pytest.raises(DecodeError, match=match):
        parse_mpeg2_transport_sdp(source)


@pytest.mark.parametrize(
    ("kwargs", "error", "match"),
    [
        ({"port": 0}, ValueError, "port"),
        ({"port": True}, ValueError, "port"),
        ({"destination_address": "not-an-address"}, ValueError, "destination"),
        ({"destination_address": 1}, TypeError, "destination"),
        ({"origin_address": "not-an-address"}, ValueError, "origin"),
        ({"origin_address": 1}, TypeError, "origin"),
        ({"session_id": True}, ValueError, "session_id"),
        ({"session_version": -1}, ValueError, "session_version"),
        ({"session_name": 1}, TypeError, "session_name"),
        ({"session_name": "mission\r\na=sendonly"}, ValueError, "session_name"),
        ({"ttl": 256}, ValueError, "ttl"),
        ({"destination_address": "239.1.1.1", "ttl": None}, ValueError, "ttl"),
        ({"destination_address": "192.0.2.20", "ttl": 1}, ValueError, "ttl"),
        ({"destination_address": "ff15::1", "ttl": 1}, ValueError, "ttl"),
    ],
)
def test_sdp_constructor_rejects_unsafe_or_invalid_fields(
    kwargs: dict[str, object], error: type[Exception], match: str
) -> None:
    values: dict[str, object] = {
        "destination_address": "192.0.2.20",
        "port": 5004,
        "origin_address": "192.0.2.10",
        "session_id": 1,
        "session_version": 1,
        "session_name": "mission",
    }
    values.update(kwargs)

    with pytest.raises(error, match=match):
        MPEG2TransportSessionDescription(**values)  # type: ignore[arg-type]
