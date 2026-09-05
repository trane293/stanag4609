# FFmpeg and GStreamer

The pure-Python core owns KLV, MPEG-TS structure, timing, validation, and
transform decisions. Mature media runtimes remain valuable at the edges for
codec decode/encode, device input, and browser-compatible output.

## FFmpeg support today

The reference player invokes FFmpeg to create H.264/AAC media for a browser.
The ArcGIS CSV multiplexer uses FFmpeg to remux ordinary input video/audio,
then the Python transport layer injects KLVA and signalling. Existing media
elementary streams are copied rather than decoded and re-encoded.

```console
stanag4609-mux-esri raw_video.mpeg metadata.csv mission.ts --ffmpeg ffmpeg
stanag4609-player mission.ts --ffmpeg ffmpeg
```

The optional PyAV adapter exposes FFmpeg codec decoding for ST 1001 audio while
keeping compressed-frame reconstruction and profile validation in Python:

```console
python -m pip install 'stanag4609[audio-pyav]'
```

See [audio](AUDIO.md) for the supported codec profiles and API.

## Pipe-oriented integration

Incremental demuxers and transformers accept arbitrary byte chunks, so an
application can read an FFmpeg/GStreamer pipe or socket without waiting for a
complete file. Preserve the library's output ordering and call `finish()` when
the finite upstream closes. For a live source, call the pipeline tick/flush API
required by the chosen component so PSI/PCR cadence and buffered data remain
observable.

Do not route binary transport bytes through text-mode pipes. A production
bridge must also handle subprocess failure, stderr draining, cancellation, and
bounded buffering.

## GStreamer integration status

A native GStreamer element and packaged Python adapter are planned, not yet
implemented. Applications can bridge today with `appsink`/`appsrc` or file
descriptors:

1. An input pipeline provides MPEG-TS bytes to the Python transformer.
2. The transformer emits remuxed transport and independent metadata events.
3. An output pipeline accepts remuxed bytes, while analytics/GIS sinks consume
   sidecar events independently.

The future first-party adapter will define caps negotiation, clocks and
segments, flush/EOS handling, discontinuities, backpressure, and bus-error
propagation. Those behaviors need integration tests against supported
GStreamer versions before the documentation presents copy-paste launch lines.

## Choose the boundary

| Need | Recommended boundary |
| --- | --- |
| Preserve and edit KLV in MPEG-TS | Python live transformer |
| Decode ST 1001 audio in process | Optional PyAV adapter |
| Decode video for inference | Application's FFmpeg, PyAV, or GStreamer adapter |
| Feed cameras, hardware codecs, or RTP graphs | GStreamer at the application edge |
| Prepare seekable browser playback | Bundled FFmpeg-backed player path |
