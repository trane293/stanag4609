from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from stanag4609.player import soak as soak_module


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def perf_counter(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


class _FakeBroadcast:
    def __init__(self, *, media: bool) -> None:
        self.media = media

    def poll(self, *, after_id: int, timeout: float) -> SimpleNamespace:
        assert after_id == -1
        assert timeout == 0
        items = ((2, b"abc"), (3, b"defg")) if self.media else ((5, object()),)
        return SimpleNamespace(items=items, dropped=2 if self.media else 5)


class _FakeGateway:
    instances: ClassVar[list[_FakeGateway]] = []
    options: ClassVar[list[dict[str, object]]] = []
    fail_instance: ClassVar[int | None] = None
    clock: ClassVar[_Clock]

    def __init__(self, **options: object) -> None:
        type(self).instances.append(self)
        type(self).options.append(options)
        self.index = len(type(self).instances)
        self.input_bytes = 0
        self.metadata = _FakeBroadcast(media=False)
        self.media = _FakeBroadcast(media=True)
        self.closed = False

    @property
    def stats(self) -> SimpleNamespace:
        return SimpleNamespace(
            input_bytes=self.input_bytes,
            metadata_samples=self.input_bytes // 188,
            media_fragments=self.input_bytes // 188,
        )

    def start(self) -> None:
        pass

    def feed(self, data: bytes) -> tuple[()]:
        type(self).clock.now += 0.05
        if type(self).fail_instance == self.index:
            raise RuntimeError("injected pipe failure")
        self.input_bytes += len(data)
        return ()

    def finish(self, *, timeout: float) -> tuple[()]:
        assert timeout == 60
        type(self).clock.now += 0.1
        return ()

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_runtime(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    _FakeGateway.instances = []
    _FakeGateway.options = []
    _FakeGateway.fail_instance = None
    _FakeGateway.clock = clock
    monkeypatch.setattr(soak_module, "LivePlayerGateway", _FakeGateway)
    monkeypatch.setattr(soak_module, "_ffmpeg_version", lambda _ffmpeg: "ffmpeg test")
    monkeypatch.setattr(soak_module.time, "perf_counter", clock.perf_counter)
    monkeypatch.setattr(soak_module.time, "sleep", clock.sleep)
    return clock


def test_soak_runs_paced_isolated_reconnect_epochs(tmp_path: Path, fake_runtime: _Clock) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"a" * 376)

    result = soak_module.soak_live_player(
        source,
        epochs=2,
        playback_rate=2,
        source_duration_seconds=2,
        chunk_bytes=188,
        media_fragments=2,
        metadata_samples=1,
        program_number=7,
    )

    assert result.schema_version == 1
    assert result.passed
    assert result.requested_epochs == result.completed_epochs == 2
    assert result.elapsed_seconds == pytest.approx(2.2)
    assert result.total_input_bytes == 752
    assert result.total_input_chunks == 4
    assert result.total_metadata_samples == 4
    assert result.total_media_fragments == 4
    assert result.maximum_pacing_lag_seconds == 0
    assert len(result.epochs) == 2
    assert all(epoch.passed for epoch in result.epochs)
    assert all(epoch.retained_media_bytes == 7 for epoch in result.epochs)
    assert len(_FakeGateway.instances) == 2
    assert _FakeGateway.instances[0] is not _FakeGateway.instances[1]
    assert _FakeGateway.options[0]["program_number"] == 7
    assert sum(fake_runtime.sleeps) == pytest.approx(1.8)


def test_soak_preserves_failed_epoch_and_stops_campaign(
    tmp_path: Path, fake_runtime: _Clock
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"a" * 376)
    _FakeGateway.fail_instance = 2

    result = soak_module.soak_live_player(
        source,
        epochs=3,
        playback_rate=100,
        source_duration_seconds=2,
        chunk_bytes=188,
    )

    assert not result.passed
    assert result.completed_epochs == 1
    assert len(result.epochs) == 2
    assert result.epochs[1].error == "RuntimeError: injected pipe failure"
    assert _FakeGateway.instances[1].closed
    assert len(_FakeGateway.instances) == 2


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"epochs": 0}, "epochs"),
        ({"playback_rate": 0}, "playback_rate"),
        ({"source_duration_seconds": float("nan")}, "source_duration_seconds"),
        ({"chunk_bytes": 187}, "chunk_bytes"),
        ({"media_fragments": 1}, "media_fragments"),
        ({"metadata_samples": 0}, "metadata_samples"),
        ({"program_number": 0}, "program_number"),
    ],
)
def test_soak_rejects_invalid_configuration(
    tmp_path: Path,
    fake_runtime: _Clock,
    arguments: dict[str, int | float],
    message: str,
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")

    with pytest.raises((TypeError, ValueError), match=message):
        soak_module.soak_live_player(source, source_duration_seconds=1, **arguments)


def test_soak_requires_probe_or_explicit_duration(
    tmp_path: Path, fake_runtime: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    monkeypatch.setattr(soak_module, "_source_duration", lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="source duration is unavailable"):
        soak_module.soak_live_player(source)


def test_soak_cli_writes_failure_report_and_returns_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    output = tmp_path / "result.json"
    result = SimpleNamespace(passed=False, to_dict=lambda: {"passed": False})
    monkeypatch.setattr(soak_module, "soak_live_player", lambda *_args, **_kwargs: result)

    status = soak_module.main([str(source), "--output", str(output)])

    assert status == 1
    assert json.loads(output.read_text(encoding="utf-8")) == {"passed": False}
    assert json.loads(capsys.readouterr().out) == {"passed": False}
