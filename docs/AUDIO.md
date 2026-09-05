# Audio streams and decoding

The core library discovers every audio elementary stream independently and
preserves its PES bytes through metadata transforms. MISB ST 1001.1 permits
MPEG-1 Layer II, MPEG-2 Layer II, and MPEG-2 AAC-LC. `AudioPESFrameParser`
reconstructs those compressed codec frames from arbitrary PES and network
boundaries, and assigns exact presentation time without any third-party
dependency.

For a finite transport, `stanag4609-verify` runs the same parser over every
declared ST 1001 audio stream. Its JSON stream inventory reports complete
frames, samples, cumulative sample duration, PTS coverage, sample rates, and
channel counts, while malformed headers and incomplete final frames become
actionable error findings.

Use one parser and decoder per PID because elementary-stream byte state and
codec state are independent:

```python
from stanag4609 import AudioPESFrameParser, PyAVAudioDecoder
from stanag4609.transport import PESStreamEvent, StreamKind

parsers = {}
decoders = {}

for event in demux_events:
    if not isinstance(event, PESStreamEvent) or event.kind is not StreamKind.AUDIO:
        continue
    codec = event.audio_codec
    if codec is None:
        continue
    parser = parsers.setdefault(event.pid, AudioPESFrameParser())
    decoder = decoders.setdefault(event.pid, PyAVAudioDecoder(codec))
    for timed in parser.feed(event):
        for audio_frame in decoder.decode(timed.frame):
            audio_sink.consume(event.pid, timed.presentation_seconds, audio_frame)

for parser in parsers.values():
    parser.finish()
for decoder in decoders.values():
    for audio_frame in decoder.flush():
        audio_sink.consume_delayed(audio_frame)
```

H.222.0 says an audio PES PTS names the first access unit whose first byte
occurs in that PES. `AudioPESFrameParser` follows that rule even when the PES
begins with the continuation of an earlier frame. A PTS may also arrive before
its frame is complete. Derived times use `fractions.Fraction`, so 44.1 kHz
audio cannot accumulate floating-point drift. Values are unwrapped across the
33-bit PTS epoch; `presentation_seconds` remains `None` until the first usable
PTS. A transport discontinuity clears the derived clock without discarding
valid compressed-byte state.

For applications that already own PES timing, the lower-level
`AudioFrameParser` remains available and returns frame bytes, stream offsets,
channel counts, sample counts, and exact durations.

Install the optional backend with:

```console
pip install 'stanag4609[audio-pyav]'
```

`PyAVAudioDecoder` creates a direct FFmpeg codec context (`mp2` or `aac`) and
returns native PyAV `AudioFrame` objects. A decode call may produce zero, one,
or multiple frames, and `flush()` releases delayed output. Native frames expose
sample format, planes, sample rate, channel layout, and resampling APIs without
forcing NumPy into this package. Advanced applications can configure the
native context through `codec_context` before decoding.

PyAV is loaded only on decoder construction. The optional dependency uses the
latest compatible major line for each supported Python: PyAV 16–17 on Python
3.10 and PyAV 18.1+ on Python 3.11+. PyAV publishes binary wheels with FFmpeg
bundled for major platforms; see the [official PyAV package](https://pypi.org/project/av/)
and [codec-context documentation](https://pyav.org/docs/develop/api/codec.html).

The dedicated optional-backend CI job uses actual codec contexts for all three
ST 1001 formats. The Esri Truck FMV acceptance test carries 6,946 timestamped
AAC access units through transport demux, PES timing, frame reconstruction, and
PCM decode, producing 7,112,704 stereo samples at 48 kHz. The supplied
`Raw_Video.mpeg` additionally provided a 6,215-frame, 149.16-second MPEG-1
Layer II reconstruction acceptance run.

The adapter intentionally does not mix channels, select an output device, or
resample. Those are application policies. It also does not parse MPEG-2 Layer
II multilingual/multichannel extensions, which ST 1001 discourages for
interoperability.
