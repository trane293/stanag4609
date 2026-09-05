from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from stanag4609.errors import DecodeError, LimitExceeded, NeedMoreData
from stanag4609.klv.ber import (
    decode_ber_length,
    decode_ber_oid,
    encode_ber_length,
    encode_ber_oid,
)


@pytest.mark.parametrize(
    ("value", "encoded"),
    [(0, b"\x00"), (127, b"\x7f"), (128, b"\x81\x80"), (256, b"\x82\x01\x00")],
)
def test_ber_length_vectors(value: int, encoded: bytes) -> None:
    assert encode_ber_length(value) == encoded
    assert decode_ber_length(encoded) == (value, len(encoded))


@given(st.integers(min_value=0, max_value=2**64 - 1))
def test_ber_length_round_trip(value: int) -> None:
    encoded = encode_ber_length(value)
    assert decode_ber_length(encoded) == (value, len(encoded))


@pytest.mark.parametrize("encoded", [b"\x80", b"\x81\x7f", b"\x82\x00\x80"])
def test_ber_length_rejects_indefinite_and_nonminimal(encoded: bytes) -> None:
    with pytest.raises(DecodeError):
        decode_ber_length(encoded)


def test_ber_length_reports_partial_and_limits() -> None:
    with pytest.raises(NeedMoreData):
        decode_ber_length(b"\x82\x01")
    with pytest.raises(LimitExceeded):
        decode_ber_length(b"\x82\x01\x00", max_value=255)


@pytest.mark.parametrize(
    ("value", "encoded"),
    [(0, b"\x00"), (1, b"\x01"), (127, b"\x7f"), (128, b"\x81\x00"), (16_383, b"\xff\x7f")],
)
def test_ber_oid_vectors(value: int, encoded: bytes) -> None:
    assert encode_ber_oid(value) == encoded
    assert decode_ber_oid(encoded) == (value, len(encoded))


@given(st.integers(min_value=0, max_value=2**63 - 1))
def test_ber_oid_round_trip(value: int) -> None:
    encoded = encode_ber_oid(value)
    assert decode_ber_oid(encoded) == (value, len(encoded))


def test_ber_oid_rejects_nonminimal_unterminated_and_oversized() -> None:
    with pytest.raises(DecodeError):
        decode_ber_oid(b"\x80\x00")
    with pytest.raises(NeedMoreData):
        decode_ber_oid(b"\x81")
    with pytest.raises(LimitExceeded):
        decode_ber_oid(b"\x81\x00", max_value=127)


@pytest.mark.parametrize("function", [encode_ber_length, encode_ber_oid])
def test_ber_encoders_reject_invalid_values(function: object) -> None:
    with pytest.raises(ValueError):
        function(-1)  # type: ignore[operator]
    with pytest.raises(TypeError):
        function(True)  # type: ignore[operator]
