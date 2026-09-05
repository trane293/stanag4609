"""Inspect every compressed ST 1001 audio frame in an MPEG-TS file."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path

from stanag4609 import AudioPESFrameParser
from stanag4609.transport import PESStreamEvent, StreamKind, TransportDemuxer
from stanag4609.transport.demux import DemuxEvent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()

    demuxer = TransportDemuxer()
    audio_parsers: dict[int, AudioPESFrameParser] = {}
    frame_counts: dict[int, int] = {}

    def consume(events: Iterable[DemuxEvent]) -> None:
        for event in events:
            if not isinstance(event, PESStreamEvent) or event.kind is not StreamKind.AUDIO:
                continue
            if event.audio_codec is None:
                continue
            audio = audio_parsers.setdefault(event.pid, AudioPESFrameParser())
            for timed in audio.feed(event):
                frame = timed.frame
                frame_counts[event.pid] = frame_counts.get(event.pid, 0) + 1
                print(
                    event.pid,
                    frame.offset,
                    frame.codec.value,
                    frame.sample_rate,
                    frame.channel_count,
                    float(frame.duration_seconds),
                    (
                        None
                        if timed.presentation_seconds is None
                        else float(timed.presentation_seconds)
                    ),
                )

    with args.input.open("rb") as source:
        while chunk := source.read(64 * 1024):
            consume(demuxer.feed(chunk))
    consume(demuxer.finish())
    for audio in audio_parsers.values():
        audio.finish()
    print("frame counts:", frame_counts)


if __name__ == "__main__":
    main()
