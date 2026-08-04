#!/usr/bin/env python3
"""Launch the documentation index carried by a deployment pack."""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import sys
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from deploy.checks import run_component_checks

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class _DocsHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: object, bundle_root: Path, **kwargs: object) -> None:
        self.bundle_root = bundle_root
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/__pack/checks":
            super().do_GET()
            return
        try:
            manifest = json.loads(
                (self.bundle_root / "pack-manifest.json").read_text(encoding="utf-8")
            )
            checks = [check.as_dict() for check in run_component_checks(manifest, self.bundle_root)]
            payload = json.dumps({"checks": checks}, sort_keys=True).encode("utf-8")
            self.send_response(200)
        except (OSError, ValueError) as exc:
            payload = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main(argv: list[str] | None = None) -> int:
    bundle_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        prog="launch-docs.py",
        description="Serve this bundle's documentation from a local HTTP address.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="port to use; 0 chooses a free port")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="allow a non-loopback bind (the docs are otherwise local-only)",
    )
    parser.add_argument("--check", action="store_true", help="validate the docs payload and exit")
    args = parser.parse_args(argv)

    index = bundle_root / "docs-index.html"
    source = bundle_root / "source"
    if not index.is_file() or not source.is_dir():
        print("launch-docs: bundle documentation payload is incomplete", file=sys.stderr)
        return 1
    if args.check:
        print(f"Documentation ready: {index}")
        return 0
    if args.host not in _LOOPBACK_HOSTS and not args.allow_network:
        parser.error("a non-loopback --host requires --allow-network")

    handler = functools.partial(
        _DocsHandler,
        directory=str(bundle_root),
        bundle_root=bundle_root,
    )
    try:
        server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        print(f"launch-docs: cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    display_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    url = f"http://{display_host}:{server.server_port}/docs-index.html"
    print(f"Documentation: {url}")
    print("Press Ctrl-C to stop.")
    if not args.no_browser and not webbrowser.open(url):
        print("No browser opened; paste the URL above into a browser.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDocumentation server stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
