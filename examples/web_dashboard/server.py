"""Serve the tutorial video, map, telemetry panel, and activity feed."""

from __future__ import annotations

import argparse
import shutil
import tempfile
import webbrowser
from collections.abc import Sequence
from contextlib import suppress
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from stanag4609.player.server import (
    PlayerHTTPRequestHandler,
    prepare_player_assets,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the stanag4609 custom web-dashboard tutorial"
    )
    parser.add_argument("source", type=Path, help="input MPEG-2 transport stream")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")

    with tempfile.TemporaryDirectory(prefix="stanag4609-web-tutorial-") as temporary:
        assets = prepare_player_assets(args.source, temporary)
        shutil.copyfile(Path(__file__).with_name("index.html"), assets.root / "index.html")
        handler = partial(PlayerHTTPRequestHandler, directory=str(assets.root))
        with ThreadingHTTPServer((args.host, args.port), handler) as server:
            url = f"http://{args.host}:{server.server_port}/"
            print(f"FMV dashboard: {url}", flush=True)
            if not args.no_open:
                webbrowser.open(url)
            with suppress(KeyboardInterrupt):
                server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
