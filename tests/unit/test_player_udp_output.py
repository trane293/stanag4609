from __future__ import annotations

import json
import socket
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from stanag4609.player.server import PlayerHTTPRequestHandler
from stanag4609.player.udp_output import (
    UDPDestination,
    UDPOutputController,
    UDPOutputRelay,
    parse_udp_destination,
)


def _packets(count: int) -> bytes:
    return b"".join(bytes((0x47, index & 0xFF)) + bytes(186) for index in range(count))


class _Socket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False
        self.error: OSError | None = None

    def sendto(self, data: bytes, destination: tuple[str, int]) -> int:
        if self.error is not None:
            raise self.error
        self.sent.append((data, destination))
        return len(data)

    def close(self) -> None:
        self.closed = True


def test_parse_udp_destination_requires_bounded_ip_literal() -> None:
    assert parse_udp_destination("127.0.0.1:5000") == UDPDestination("127.0.0.1", 5000)
    assert parse_udp_destination("[2001:0db8::1]:1234") == UDPDestination("2001:db8::1", 1234)
    for value in ("localhost:1", "0.0.0.0:1", "[::]:1", "127.0.0.1:0", " 1.1.1.1:1"):
        with pytest.raises(ValueError):
            parse_udp_destination(value)


def test_udp_relay_packetizes_only_while_enabled_and_reports_counts() -> None:
    transport = _Socket()
    families: list[tuple[int, int]] = []

    def socket_factory(family: int, kind: int) -> socket.socket:
        families.append((family, kind))
        return transport  # type: ignore[return-value]

    destination = UDPDestination("127.0.0.1", 5000)
    relay = UDPOutputRelay(destination, socket_factory=socket_factory)
    relay.begin_epoch()
    relay.feed(_packets(7))
    assert transport.sent == []
    relay.set_enabled(True)
    relay.feed(_packets(8))
    relay.finish_epoch()

    assert families == [(socket.AF_INET, socket.SOCK_DGRAM)]
    assert [len(data) for data, _destination in transport.sent] == [7 * 188, 188]
    assert all(sent_destination == ("127.0.0.1", 5000) for _, sent_destination in transport.sent)
    status = relay.status(mode="test")
    assert (status.epochs, status.datagrams, status.bytes) == (1, 2, 8 * 188)
    assert not status.active
    relay.close()
    assert transport.closed


def test_udp_send_error_disables_output_without_stopping_packetization() -> None:
    transport = _Socket()
    relay = UDPOutputRelay(
        UDPDestination("127.0.0.1", 5000),
        packets_per_datagram=1,
        socket_factory=lambda _family, _kind: transport,  # type: ignore[arg-type]
    )
    relay.begin_epoch()
    relay.set_enabled(True)
    transport.error = OSError("network down")
    relay.feed(_packets(1))
    assert not relay.status(mode="live").enabled
    assert relay.status(mode="live").active
    assert relay.status(mode="live").error == "OSError: network down"
    transport.error = None
    relay.feed(_packets(1))
    relay.finish_epoch()


def test_recorded_controller_replays_from_start_with_pacing(tmp_path: Path) -> None:
    source = tmp_path / "recording.ts"
    source.write_bytes(_packets(8))
    transport = _Socket()
    relay = UDPOutputRelay(
        UDPDestination("127.0.0.1", 5000),
        socket_factory=lambda _family, _kind: transport,  # type: ignore[arg-type]
    )
    now = [0.0]

    def wait(seconds: float) -> bool:
        now[0] += seconds
        return False

    controller = UDPOutputController(
        relay.destination,
        live=False,
        source=source,
        source_duration_seconds=4,
        relay=relay,
        monotonic=lambda: now[0],
        wait=wait,
    )
    controller.start()
    assert controller._thread is not None
    controller._thread.join(timeout=1)

    assert b"".join(data for data, _destination in transport.sent) == source.read_bytes()
    assert now[0] == pytest.approx(4)
    assert not controller.status().active
    controller.close()


def test_live_controller_only_controls_an_existing_epoch() -> None:
    transport = _Socket()
    relay = UDPOutputRelay(
        UDPDestination("::1", 5000),
        packets_per_datagram=1,
        socket_factory=lambda _family, _kind: transport,  # type: ignore[arg-type]
    )
    controller = UDPOutputController(relay.destination, live=True, relay=relay)
    controller.begin_live()
    assert controller.start().enabled
    controller.feed_live(_packets(1))
    assert controller.stop().enabled is False
    controller.feed_live(_packets(1))
    controller.finish_live()
    assert len(transport.sent) == 1
    assert controller.status().mode == "live tee"
    controller.close()


def test_player_udp_control_is_fixed_destination_and_token_protected(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("player", encoding="utf-8")

    class Controller:
        def __init__(self) -> None:
            self.enabled = False

        def status(self):  # type: ignore[no-untyped-def]
            return UDPOutputRelayStatus(self.enabled)

        def start(self):  # type: ignore[no-untyped-def]
            self.enabled = True
            return self.status()

        def stop(self):  # type: ignore[no-untyped-def]
            self.enabled = False
            return self.status()

    class UDPOutputRelayStatus:
        def __init__(self, enabled: bool) -> None:
            self.enabled = enabled

        def to_dict(self) -> dict[str, object]:
            return {
                "configured": True,
                "destination": "127.0.0.1:5000",
                "mode": "test",
                "enabled": self.enabled,
                "active": True,
                "epochs": 1,
                "datagrams": 0,
                "bytes": 0,
                "error": None,
            }

    controller = Controller()
    handler = partial(
        PlayerHTTPRequestHandler,
        directory=str(tmp_path),
        udp_output=controller,  # type: ignore[arg-type]
        control_token="secret",
        allowed_hosts=("127.0.0.1",),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/output/udp")
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 200
        assert payload["destination"] == "127.0.0.1:5000"
        assert payload["control_token"] == "secret"

        connection.request("POST", "/output/udp/start")
        response = connection.getresponse()
        assert response.status == 403
        response.read()

        connection.request(
            "POST",
            "/output/udp/start",
            headers={"X-STANAG4609-Control": "secret", "Sec-Fetch-Site": "cross-site"},
        )
        response = connection.getresponse()
        assert response.status == 403
        response.read()

        connection.request(
            "POST",
            "/output/udp/start",
            headers={"X-STANAG4609-Control": "secret", "Sec-Fetch-Site": "same-origin"},
        )
        response = connection.getresponse()
        assert json.loads(response.read())["enabled"] is True
        assert response.status == 200
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
