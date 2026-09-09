"""Operator-only staging/verification CLI, with no rendering imports.

Use a separate manifest/cache/data root for candidate acceptance. Disabled
entries are never overridden by this tool. Offline preparation is explicit;
ordinary ``main.py --no-sync`` only verifies previously prepared releases.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from launcher.cache import RepositoryCache
from launcher.errors import LauncherError
from launcher.manifest import GameEntry, load_manifest
from launcher.paths import MANIFEST_FILE, default_cache_root
from launcher.preparation import PreparationService, PreparedGame, launch_metadata
from launcher.runtimes import RuntimeStore
from launcher.status import GameState, GameStatus


def _explicit_checkout(
    preparation: PreparationService, entry: GameEntry, checkout: Path, *,
    verify_only: bool, allow_download: bool,
) -> tuple[GameState, PreparedGame | None, bool]:
    """Use the existing preparation API without fetching/cleaning candidate work."""
    try:
        if verify_only:
            game = preparation.resolve(entry, checkout)
        else:
            game = preparation.prepare(entry, checkout, allow_download=allow_download)
        complete = preparation.matches(entry, checkout, include_source=True)
        status = GameStatus.READY if complete else GameStatus.CACHED_OFFLINE
        detail = "explicit checkout verified" if complete else "last-good playable; candidate source/metadata not prepared"
        return GameState(entry.id, status, detail), game, complete
    except LauncherError as exc:
        # A failed candidate is not a successful tool run, even when its old
        # native receipt still points at a healthy, independently stored build.
        try:
            game = preparation.resolve(entry, checkout)
        except LauncherError:
            return GameState(entry.id, GameStatus.UNAVAILABLE, str(exc)), None, False
        return GameState(entry.id, GameStatus.CACHED_OFFLINE, str(exc)), game, False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--runtime-cache", type=Path, default=None)
    parser.add_argument("--game-data-root", type=Path, default=None)
    parser.add_argument("--game", action="append", default=[], help="select a launchable id (repeatable)")
    parser.add_argument(
        "--checkout", type=Path,
        help="prepare/verify one explicit candidate checkout, including dirty files; no git sync",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-only", action="store_true", help="disk-only readiness; no subprocesses or writes")
    mode.add_argument("--offline", action="store_true", help="prepare cached sources/runtimes without downloading")
    mode.add_argument("--rollback", type=Path, help="restore a managed rollback checkout; requires one --game")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr)
    try:
        manifest = load_manifest(args.manifest)
        unknown = set(args.game) - {entry.id for entry in manifest.launchable}
        if unknown:
            parser.error(f"unknown or disabled game ids: {', '.join(sorted(unknown))}")
        if args.rollback and len(args.game) != 1:
            parser.error("--rollback requires exactly one --game")
        if args.checkout is not None:
            if len(args.game) != 1 or args.cache is None or args.game_data_root is None:
                parser.error("--checkout requires exactly one --game plus explicit --cache and --game-data-root")
            if args.rollback is not None:
                parser.error("--checkout cannot be combined with --rollback")
        root = args.cache or default_cache_root()
        preparation = PreparationService(
            root / "prepared", runtimes=RuntimeStore(args.runtime_cache), data_root=args.game_data_root,
        )
        cache = RepositoryCache(root, preparation=preparation)
        success = True
        for entry in manifest.launchable:
            if args.game and entry.id not in args.game:
                continue
            game = None
            if args.checkout is not None:
                state, game, complete = _explicit_checkout(
                    preparation, entry, args.checkout, verify_only=args.verify_only,
                    allow_download=not args.offline,
                )
            elif args.verify_only:
                state = cache.verify_only(entry)
                complete = state.status.is_playable and preparation.matches(
                    entry, cache.checkout_path(entry), include_source=True,
                )
            elif args.offline:
                state = cache.prepare_cached(entry, allow_download=False)
                complete = state.status is GameStatus.READY
            elif args.rollback:
                state = cache.rollback(entry, args.rollback)
                complete = state.status is GameStatus.READY
            else:
                state = cache.sync(entry)
                complete = state.status is GameStatus.READY
            record: dict[str, object] = {
                "game": entry.id, "status": state.status.value, "detail": state.detail,
                "requested_release_prepared": complete,
            }
            if args.checkout is not None:
                record["source_checkout"] = str(args.checkout.resolve())
            if state.status.is_playable:
                game = game if game is not None else cache.prepared_game(entry)
                record.update(
                    executable=str(game.executable), checkout=str(game.checkout),
                    game_data_dir=str(game.data_dir),
                    launch=launch_metadata(game.entry),
                )
            print(json.dumps(record), flush=True)
            # Keeping last-good playable is not "successfully prepared the
            # candidate". Operators must see a failing exit code in that case.
            success = success and complete
        return 0 if success else 1
    except LauncherError as exc:
        print(f"Preparation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
