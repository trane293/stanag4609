from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from stanag4609.player import benchmark as benchmark_module


class _FakeBroadcast:
    def __init__(self, items: tuple[tuple[int, object], ...], dropped: int) -> None:
        self._result = SimpleNamespace(items=items, dropped=dropped)

    def poll(self, *, after_id: int, timeout: float) -> SimpleNamespace:
        assert after_id == -1
        assert timeout == 0
        return self._result


class _FakeGateway:
    options: ClassVar[dict[str, object]] = {}

    def __init__(self, **options: object) -> None:
        type(self).options = options
        self.stats = SimpleNamespace(metadata_samples=7, media_fragments=5)
        self.media = _FakeBroadcast(((3, b"abc"), (4, b"defg")), dropped=3)
        self.metadata = _FakeBroadcast(((5, object()), (6, object())), dropped=5)
        self.input = bytearray()
        self.closed = False

    def start(self) -> None:
        pass

    def feed(self, data: bytes) -> tuple[()]:
        self.input.extend(data)
        return ()

    def finish(self, *, timeout: float) -> tuple[()]:
        assert timeout == 60
        return ()

    def close(self) -> None:
        self.closed = True


def test_live_player_benchmark_reports_identity_throughput_and_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(bytes(range(256)) * 10)
    monkeypatch.setattr(benchmark_module, "LivePlayerGateway", _FakeGateway)
    monkeypatch.setattr(benchmark_module, "_source_duration", lambda *_args, **_kwargs: 12.5)
    monkeypatch.setattr(benchmark_module, "_ffmpeg_version", lambda _ffmpeg: "ffmpeg test")

    result = benchmark_module.benchmark_live_player(
        source,
        chunk_bytes=188,
        media_fragments=2,
        metadata_samples=2,
        program_number=7,
    )

    assert result.schema_version == 1
    assert result.source_bytes == 2560
    assert result.source_sha256 == (
        "e392378f849d67bbb1a7bbec84f1098ae3faa751049c009a850130ce6073d91a"
    )
    assert result.source_duration_seconds == 12.5
    assert result.input_chunks == 14
    assert result.input_megabits_per_second > 0
    assert result.media_seconds_per_wall_second is not None
    assert result.retained_media_fragments == 2
    assert result.dropped_media_fragments == 3
    assert result.retained_media_bytes == 7
    assert result.retained_metadata_samples == 2
    assert result.dropped_metadata_samples == 5
    assert result.ffmpeg == "ffmpeg test"
    assert _FakeGateway.options["max_input_chunk_bytes"] == 188
    assert _FakeGateway.options["program_number"] == 7


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"chunk_bytes": 187}, "chunk_bytes"),
        ({"media_fragments": 1}, "media_fragments"),
        ({"metadata_samples": 0}, "metadata_samples"),
        ({"program_number": 0}, "program_number"),
    ],
)
def test_live_player_benchmark_rejects_invalid_bounds(
    tmp_path: Path, arguments: dict[str, int], message: str
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")

    with pytest.raises((TypeError, ValueError), match=message):
        benchmark_module.benchmark_live_player(source, **arguments)


def test_live_player_benchmark_cli_refuses_to_replace_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.ts"
    source.write_bytes(b"source")
    output = tmp_path / "result.json"
    output.write_text("keep", encoding="utf-8")
    called = False

    def unexpected_benchmark(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(benchmark_module, "benchmark_live_player", unexpected_benchmark)

    with pytest.raises(SystemExit) as raised:
        benchmark_module.main([str(source), "--output", str(output)])

    assert raised.value.code == 2
    assert not called
    assert output.read_text(encoding="utf-8") == "keep"
