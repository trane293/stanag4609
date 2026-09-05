"""Bounded incremental Universal KLV stream parsing."""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO

from stanag4609.errors import DecodeError, NeedMoreData, TruncatedData
from stanag4609.klv.ber import decode_ber_length
from stanag4609.klv.key import SMPTE_UL_LENGTH, SMPTE_UL_PREFIX, validate_klv_key
from stanag4609.klv.model import KLVPacket


class KLVStreamParser:
    """Incrementally reconstruct fixed-width-key KLV packets.

    Input chunks may split the key, BER length, or value at any byte boundary.
    Completed bytes are released immediately, and declared values are bounded.
    """

    def __init__(
        self,
        *,
        key_length: int = 16,
        key_prefix: bytes | None = b"\x06\x0e\x2b\x34",
        canonical: bool = True,
        recover: bool = False,
        max_value_length: int = 64 * 1024 * 1024,
        validate_smpte_keys: bool | None = None,
    ) -> None:
        if key_length < 1:
            raise ValueError("key_length must be positive")
        if key_prefix is not None and not 1 <= len(key_prefix) <= key_length:
            raise ValueError("key_prefix must fit within the key")
        if recover and key_prefix is None:
            raise ValueError("recovery requires a key_prefix")
        if max_value_length < 0:
            raise ValueError("max_value_length cannot be negative")
        if validate_smpte_keys is None:
            compatible_prefix = key_prefix is not None and (
                key_prefix.startswith(SMPTE_UL_PREFIX)
                or SMPTE_UL_PREFIX.startswith(key_prefix)
            )
            validate_smpte_keys = key_length == SMPTE_UL_LENGTH and compatible_prefix
        if validate_smpte_keys and key_length != SMPTE_UL_LENGTH:
            raise ValueError("SMPTE key validation requires 16-byte keys")
        self.key_length = key_length
        self.key_prefix = key_prefix
        self.canonical = canonical
        self.recover = recover
        self.max_value_length = max_value_length
        self.validate_smpte_keys = validate_smpte_keys
        self._buffer = bytearray()
        self._offset = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def stream_offset(self) -> int:
        return self._offset

    def feed(self, data: bytes | bytearray | memoryview) -> list[KLVPacket]:
        """Consume a chunk and return every newly completed packet."""
        self._buffer.extend(data)
        packets: list[KLVPacket] = []
        while True:
            if not self._align_key():
                break
            if len(self._buffer) < self.key_length:
                break
            if self.validate_smpte_keys:
                validate_klv_key(self._buffer[: self.key_length])
            if len(self._buffer) < self.key_length + 1:
                break
            try:
                value_length, length_used = decode_ber_length(
                    self._buffer,
                    self.key_length,
                    canonical=self.canonical,
                    max_value=self.max_value_length,
                )
            except NeedMoreData:
                break
            total = self.key_length + length_used + value_length
            if len(self._buffer) < total:
                break
            packet_offset = self._offset
            key = bytes(self._buffer[: self.key_length])
            length_octets = bytes(
                self._buffer[self.key_length : self.key_length + length_used]
            )
            value = bytes(self._buffer[self.key_length + length_used : total])
            del self._buffer[:total]
            self._offset += total
            packets.append(KLVPacket(key, value, length_octets, packet_offset))
        return packets

    def finish(self) -> list[KLVPacket]:
        """Signal end of input and reject an incomplete trailing structure."""
        packets = self.feed(b"")
        if self._buffer:
            raise TruncatedData(
                f"stream ended with {len(self._buffer)} incomplete byte(s) "
                f"at offset {self._offset}"
            )
        return packets

    def _align_key(self) -> bool:
        if not self._buffer:
            return False
        if self.key_prefix is None:
            return True
        if len(self._buffer) < len(self.key_prefix):
            if self.key_prefix.startswith(self._buffer):
                return True
        elif self._buffer.startswith(self.key_prefix):
            return True
        if not self.recover:
            observed = bytes(self._buffer[: len(self.key_prefix)]).hex(" ")
            raise DecodeError(
                f"expected KLV key prefix {self.key_prefix.hex(' ')} at offset "
                f"{self._offset}, observed {observed}"
            )
        index = self._buffer.find(self.key_prefix, 1)
        if index >= 0:
            del self._buffer[:index]
            self._offset += index
            return True
        keep = min(len(self._buffer), len(self.key_prefix) - 1)
        discard = len(self._buffer) - keep
        if discard:
            del self._buffer[:discard]
            self._offset += discard
        return False


def iter_klv(
    stream: BinaryIO,
    *,
    chunk_size: int = 64 * 1024,
    parser: KLVStreamParser | None = None,
) -> Iterator[KLVPacket]:
    """Yield KLV packets from a binary file-like object."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    active = parser or KLVStreamParser()
    while chunk := stream.read(chunk_size):
        yield from active.feed(chunk)
    yield from active.finish()
