# Installation

## Install the release

`stanag4609` supports Python 3.10 through 3.14. A virtual environment keeps it
separate from system packages:

=== "macOS and Linux"

    ```console
    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install stanag4609
    ```

=== "Windows PowerShell"

    ```powershell
    py -m venv .venv
    .venv\Scripts\Activate.ps1
    python -m pip install stanag4609
    ```

Check the installed version and command-line tools:

```console
python -m pip show stanag4609
stanag4609-verify --help
```

The core package has no required third-party Python dependencies.

## Install optional integrations

Install an extra only when your application needs its runtime:

| Extra | Use it for |
| --- | --- |
| `video-pyav` | Decode video frames through PyAV and FFmpeg |
| `audio-pyav` | Decode supported compressed audio to PCM |
| `ai-ultralytics` | Run Ultralytics prediction and tracking |
| `ai-onnx` | Run an ONNX model with ONNX Runtime |
| `ai-triton-http` | Call NVIDIA Triton over HTTP |
| `ai-triton-grpc` | Call NVIDIA Triton over gRPC |

For example:

```console
python -m pip install 'stanag4609[video-pyav,ai-ultralytics]'
```

## Install FFmpeg

The protocol library can parse and transform transport streams without FFmpeg.
The reference player, demo generator, CSV muxer, and media-decoding adapters use
FFmpeg for codec work.

Confirm that the executable is available:

```console
ffmpeg -version
```

Use the packages published by the [FFmpeg project](https://ffmpeg.org/download.html)
or your operating system's package manager.

## Upgrade

```console
python -m pip install --upgrade stanag4609
```

The API remains pre-1.0. Read the [changelog](https://github.com/trane293/stanag4609/blob/main/CHANGELOG.md)
and [API stability policy](API_STABILITY.md) before adopting a new minor release.

## Develop from source

An editable source installation is for contributors working on the repository.
Follow the [development guide](DEVELOPMENT.md) for environment setup and test
commands.

## Continue

- [Run the quickstart](QUICKSTART.md).
- [Choose a workflow](WORKFLOWS.md).
- Review [common pitfalls](COMMON_PITFALLS.md) if an existing stream behaves
  differently from the generated demo.
