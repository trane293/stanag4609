"""Explicit, bounded UDP transport output for the localhost reference player."""

from __future__ import annotations

import ipaddress
import math
import socket
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event, Lock, Thread

from stanag4609.transport.udp import UdpTransportPacketizer


@dataclass(frozen=True, slots=True)
class UDPDestination:
    """One explicit IP-literal UDP destination."""

    host: str
    port: int

    @property
    def display(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"


def parse_udp_destination(value: str) -> UDPDestination:
    """Parse ``IP:port`` or ``[IPv6]:port`` without DNS resolution."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("UDP destination must be a non-empty IP:port value")
    if value.startswith("["):
        host, separator, port_text = value[1:].partition("]:")
    else:
        host, separator, port_text = value.rpartition(":")
    if not separator or not host or not port_text:
        raise ValueError("UDP destination must be IP:port or [IPv6]:port")
    try:
        address = ipaddress.ip_address(host)
        port = int(port_text)
    except ValueError as error:
        raise ValueError("UDP destination requires an IP literal and integer port") from error
    if address.is_unspecified:
        raise ValueError("UDP destination cannot be an unspecified address")
    if not 1 <= port <= 65_535:
        raise ValueError("UDP destination port must be between 1 and 65535")
    return UDPDestination(address.compressed, port)


@dataclass(frozen=True, slots=True)
class UDPOutputStatus:
    configured: bool
    destination: str
    mode: str
    enabled: bool
    active: bool
    epochs: int
    datagrams: int
    bytes: int
    error: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class UDPOutputRelay:
    """Packetize TS continuously and conditionally forward complete datagrams."""

    def __init__(
        self,
        destination: UDPDestination,
        *,
        packets_per_datagram: int = 7,
        socket_factory: Callable[[int, int], socket.socket] = socket.socket,
    ) -> None:
        if not isinstance(destination, UDPDestination):
            raise TypeError("destination must be UDPDestination")
        family = socket.AF_INET6 if ":" in destination.host else socket.AF_INET
        self.destination = destination
        self._socket = socket_factory(family, socket.SOCK_DGRAM)
        self._packets_per_datagram = packets_per_datagram
        self._packetizer: UdpTransportPacketizer | None = None
        self._enabled = False
        self._active = False
        self._epochs = 0
        self._datagrams = 0
        self._bytes = 0
        self._error: str | None = None
        self._lock = Lock()

    def begin_epoch(self) -> None:
        with self._lock:
            if self._active:
                raise RuntimeError("UDP output epoch is already active")
            self._packetizer = UdpTransportPacketizer(
                packets_per_datagram=self._packets_per_datagram
            )
            self._active = True
            self._epochs += 1
            self._error = None

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be bool")
        with self._lock:
            self._enabled = enabled

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            if not self._active or self._packetizer is None:
                raise RuntimeError("UDP output epoch is not active")
            datagrams = self._packetizer.feed(chunk)
            enabled = self._enabled
        if enabled:
            self._send(datagrams)

    def finish_epoch(self) -> None:
        with self._lock:
            if not self._active or self._packetizer is None:
                return
            datagrams = self._packetizer.finish()
            enabled = self._enabled
            self._packetizer = None
            self._active = False
        if enabled:
            self._send(datagrams)

    def fail_epoch(self, error: Exception) -> None:
        with self._lock:
            self._packetizer = None
            self._active = False
            self._enabled = False
            self._error = f"{type(error).__name__}: {error}"

    def _record_send_error(self, error: OSError) -> None:
        with self._lock:
            self._enabled = False
            self._error = f"{type(error).__name__}: {error}"

    def _send(self, datagrams: tuple[bytes, ...]) -> None:
        for datagram in datagrams:
            try:
                sent = self._socket.sendto(
                    datagram, (self.destination.host, self.destination.port)
                )
                if sent != len(datagram):
                    raise OSError(f"short UDP send: {sent} of {len(datagram)} bytes")
            except OSError as error:
                self._record_send_error(error)
                return
            with self._lock:
                self._datagrams += 1
                self._bytes += sent

    def status(self, *, mode: str) -> UDPOutputStatus:
        with self._lock:
            return UDPOutputStatus(
                configured=True,
                destination=self.destination.display,
                mode=mode,
                enabled=self._enabled,
                active=self._active,
                epochs=self._epochs,
                datagrams=self._datagrams,
                bytes=self._bytes,
                error=self._error,
            )

    def close(self) -> None:
        self.set_enabled(False)
        self._socket.close()


class UDPOutputController:
    """Control a live tee or a restartable paced recording replay."""

    def __init__(
        self,
        destination: UDPDestination,
        *,
        live: bool,
        source: Path | None = None,
        source_duration_seconds: float | None = None,
        playback_rate: float = 1.0,
        chunk_size: int = 1_316,
        relay: UDPOutputRelay | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        if live:
            if source is not None or source_duration_seconds is not None:
                raise ValueError("live UDP output does not accept a replay source")
        elif source is None or source_duration_seconds is None:
            raise ValueError("recorded UDP output requires source and duration")
        if source_duration_seconds is not None and (
            not math.isfinite(source_duration_seconds) or source_duration_seconds <= 0
        ):
            raise ValueError("source duration must be finite and positive")
        if not math.isfinite(playback_rate) or not 0.1 <= playback_rate <= 16:
            raise ValueError("playback rate must be between 0.1 and 16")
        if chunk_size < 188:
            raise ValueError("chunk size must be at least 188")
        self.live = live
        self.source = source
        self.source_duration_seconds = source_duration_seconds
        self.playback_rate = playback_rate
        self.chunk_size = chunk_size
        self.relay = relay or UDPOutputRelay(destination)
        self._monotonic = monotonic
        self._stop = Event()
        self._wait = self._stop.wait if wait is None else wait
        self._thread: Thread | None = None

    @property
    def mode(self) -> str:
        return "live tee" if self.live else "recorded replay from start"

    def start(self) -> UDPOutputStatus:
        if self.live:
            self.relay.set_enabled(True)
            return self.status()
        if self._thread is not None and self._thread.is_alive():
            self.relay.set_enabled(True)
            return self.status()
        self._stop.clear()
        self.relay.set_enabled(True)
        self._thread = Thread(
            target=self._replay,
            name="stanag4609-udp-output",
            daemon=True,
        )
        self._thread.start()
        return self.status()

    def stop(self) -> UDPOutputStatus:
        self.relay.set_enabled(False)
        self._stop.set()
        return self.status()

    def begin_live(self) -> None:
        if not self.live:
            raise RuntimeError("recorded UDP output cannot begin a live epoch")
        self.relay.begin_epoch()

    def feed_live(self, chunk: bytes) -> None:
        if not self.live:
            raise RuntimeError("recorded UDP output cannot consume live input")
        self.relay.feed(chunk)

    def finish_live(self) -> None:
        if self.live:
            self.relay.finish_epoch()

    def _replay(self) -> None:
        assert self.source is not None and self.source_duration_seconds is not None
        try:
            size = self.source.stat().st_size
            if size == 0:
                raise ValueError("UDP replay source is empty")
            self.relay.begin_epoch()
            sent = 0
            started = self._monotonic()
            with self.source.open("rb") as stream:
                while not self._stop.is_set() and (chunk := stream.read(self.chunk_size)):
                    self.relay.feed(chunk)
                    sent += len(chunk)
                    target = sent / size * self.source_duration_seconds / self.playback_rate
                    remaining = target - (self._monotonic() - started)
                    if remaining > 0 and self._wait(remaining):
                        break
            self.relay.finish_epoch()
        except Exception as error:
            self.relay.fail_epoch(error)

    def status(self) -> UDPOutputStatus:
        return self.relay.status(mode=self.mode)

    def close(self) -> None:
        self.stop()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.relay.finish_epoch()
        self.relay.close()
