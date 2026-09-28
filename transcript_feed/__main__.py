"""CLI: --serve (page + live feed), --once (one snapshot to stdout), --watch (write a
JSON file on a loop for your own static host)."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from .feed import FeedComposer
from .server import serve


def _write_atomic(path, data):
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(data)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="claude-transcript-feed",
        description="Live web view of a Claude Code agent, from its JSONL transcript.")
    ap.add_argument("transcript", help="path to a Claude Code session .jsonl transcript")
    ap.add_argument("--label", default=None, help="header label (default: transcript filename)")
    ap.add_argument("--max-events", type=int, default=None, help="scrollback depth (default 250)")

    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true", help="serve the live page")
    mode.add_argument("--once", action="store_true", help="print one JSON snapshot")
    mode.add_argument("--watch", action="store_true", help="write --out on a loop")

    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--out", default="feed.json", help="output file for --watch")
    ap.add_argument("--every", type=float, default=2.0, help="--watch poll seconds")
    a = ap.parse_args(argv)

    composer = FeedComposer(a.transcript, label=a.label, max_events=a.max_events)

    if a.serve:
        serve(composer, host=a.host, port=a.port)
        return 0

    if a.watch:
        out = Path(a.out)
        print("watching %s -> %s every %ss (Ctrl-C to stop)" % (a.transcript, out, a.every))
        try:
            while True:
                _write_atomic(out, json.dumps(composer.build()))
                time.sleep(a.every)
        except KeyboardInterrupt:
            print("\nstopping.")
        return 0

    # default: --once
    print(json.dumps(composer.build(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
