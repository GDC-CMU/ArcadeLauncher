"""A real game fixture with a helper/grandchild that outlive or ignore their parent."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--role", choices=("game", "helper", "grandchild"), default="game")
    parser.add_argument("--exit-code", type=int, default=None)
    args = parser.parse_args()
    if args.role == "grandchild":
        if os.name != "nt":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        args.record.with_suffix(".ready").write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(60)
        return 0

    command = [
        sys.executable, str(Path(__file__).resolve()), "--record", str(args.record),
        "--role", "helper" if args.role == "game" else "grandchild",
    ]
    kwargs = {}
    if os.name == "nt" and args.role == "helper":
        # A different Windows console group must still remain in the owned Job.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, **kwargs)
    deadline = time.monotonic() + 10
    if args.role == "helper":
        ready = args.record.with_suffix(".ready")
        while not ready.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.is_file():
            return 20
        temporary = args.record.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"game": os.getppid(), "helper": os.getpid(), "grandchild": child.pid}),
            encoding="utf-8",
        )
        temporary.replace(args.record)
        if os.name != "nt":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
        return 0
    while not args.record.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not args.record.is_file():
        return 21
    if args.exit_code is not None:
        return args.exit_code
    time.sleep(60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
