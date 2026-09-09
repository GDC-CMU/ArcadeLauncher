# Native runtime and preparation contract

## Activation boundary

The shipped `data/games.json` enables all eight accepted club games. Use a
separate staged manifest/cache/data root to prove future candidates. Publish,
promote installed revisions and activate entries only after native cabinet checks.
No headless diagnostic is a claim of complete game/controller readiness.

## Public manifest schema (version 1)

Existing card fields stay unchanged. Runtime fields are strict and mutually
exclusive; unknown fields, command strings and unsupported engine tags fail
validation. Disabled entries must not carry delivery/preparation fields.

| Field | Type / constraints |
| --- | --- |
| `runtime` | `"python"` or `"godot"` (`Runtime` enum after parsing) |
| `repository` | Credential-free HTTPS repository URL, no query/fragment |
| `ref` | Branch, tag, or full 40-character commit SHA |
| `entrypoint` | Contained checkout-relative, forward-slash path; spaces supported |
| `godot_version` | Required for Godot: `"4.4.1-stable"` or `"4.5.2-stable"` (`GodotVersion` enum) |
| `startup_script` | Optional **pack-only** contained `.gd` path, supplied via `--script` |
| `python_requirements` | Optional for backward-compatible Python; required by the new Python adapter contract |

Godot entrypoints must be `.pck` or a file named `project.godot`. A PCK header
must match its exact pinned engine version (format 2 or 3); initialization is
probed during preparation. A source project is copied and imported with its own
engine in a prepared snapshot. No Mono, Wine, Web wrapper, arbitrary argv field
or export-template installation is supported/needed for this launch path.

Examples of the **runtime portion** of otherwise complete staged entries:

```json
{
  "runtime": "godot",
  "godot_version": "4.4.1-stable",
  "entrypoint": "Flappy Scotty.pck",
  "startup_script": "arcade_bootstrap.gd"
}
```

```json
{
  "runtime": "godot",
  "godot_version": "4.5.2-stable",
  "entrypoint": "project.godot"
}
```

```json
{
  "runtime": "python",
  "entrypoint": "arcade_main.py",
  "python_requirements": "requirements.txt"
}
```

Use the actual adapter filename; do not declare a bootstrap that is not shipped.
The bootstrap must preserve the game's real scenes/autoloads. Engine joypad
indices and back/menu actions are the game adapter's responsibility.

Requirements accept package specifications, optional SHA256 `--hash` options,
and contained relative `-r`/`-c` includes. Include cycles, outside paths, editable/
local/URL/VCS installs, `${...}` substitution and other pip options are rejected.
Use pinned packages (and a hash-locked transitive list where practical). An empty
file is valid for a stdlib-only new game. A floating dependency constraint is
not a promise of reproducible future resolution; an already prepared environment
is reused unchanged by its requirements + interpreter ABI fingerprint.

## Public Python APIs

All these backend modules are pygame-free.

```python
from pathlib import Path

from launcher.runtimes import RuntimeStore
from launcher.preparation import PreparationService, PreparedGame
from launcher.cache import RepositoryCache
from launcher.supervisor import build_child_command

RuntimeStore(root=None, *, target=None, downloader=None, runner=None)
store.ensure(version, *, allow_download=True) -> Path  # preparation only
store.verify(version) -> Path                         # disk only, no process

PreparationService(root, *, runtimes=None, runner=None, data_root=None)
preparation.prepare(entry, checkout, *, allow_download=True) -> PreparedGame
preparation.resolve(entry, checkout) -> PreparedGame  # disk only; can be last-good
preparation.matches(entry, checkout, *, include_source=False) -> bool

RepositoryCache(root=None, runner=None, clock=..., preparation=None)
cache.sync(entry) -> GameState                       # one startup check/candidate
cache.verify_only(entry) -> GameState                # disk only
cache.prepared_game(entry) -> PreparedGame            # disk only
cache.prepare_cached(entry, *, allow_download=False) -> GameState  # operator only
cache.rollback(entry, backup: Path) -> GameState      # operator only, offline

build_child_command(entry, checkout, *, executable=None) -> list[str]
```

`version` accepts a `GodotVersion` enum or its exact tag string. `target` is a
`RuntimePlatform`; production uses the current Linux/Windows x86_64 platform.
Download/process seams exist for offline tests, not manifest-configured code.

### Explicit candidate staging (including dirty sources)

`PreparationService.root` is the prepared artifact/dependency cache, **not**
the source checkout or data directory. All three roots must be disjoint.
`prepare()` accepts the current working files of an independent checkout with
a `.git` directory, including dirty and untracked files. It does not run git,
require a new commit, reset/clean source files, or promote an installed checkout.
The only source-checkout write is its successful `.git` preparation receipt.
Use a dedicated staged copy, not another developer's active or installed tree.

Native keys include the actual file bytes, launch metadata and target platform,
not Git HEAD. Copied bytes and preparation inputs are checked before readiness
is published. Unchanged preparations reuse the artifact. Python keys include
dependency files and interpreter ABI; Python source continues to run from the
explicit staged checkout, not an immutable native snapshot.

```python
source = Path("/staging/cache/games/flappy-scotty")
cache_root = Path("/staging/cache")
data_root = Path("/staging/saves")
preparation = PreparationService(
    cache_root / "prepared", runtimes=RuntimeStore(Path("/staging/engines")),
    data_root=data_root,
)
ready = preparation.prepare(entry, source, allow_download=False)
assert preparation.matches(entry, source, include_source=True)
# Later, offline/Play: resolve only, never prepare.
ready = preparation.resolve(entry, source)
command = build_child_command(ready.entry, ready.checkout, executable=ready.executable)
```

The equivalent CLI requires one enabled staged-manifest entry and explicit
checkout/cache/data roots; paths containing spaces must be quoted:

```text
python -m tools.prepare_games --manifest /staging/games.json --game flappy-scotty
  --checkout "/staging/cache/games/flappy-scotty" --cache /staging/cache
  --runtime-cache /staging/engines --game-data-root /staging/saves --offline
```

Replace `--offline` with `--verify-only` for disk-only verification. Omit either
flag only when provisioning is deliberately allowed. Operator verification
uses `matches(..., include_source=True)`: changed/untracked/deleted source files
cannot be reported as the requested prepared candidate just because Git HEAD
or launch metadata is unchanged. A failed candidate returns nonzero while
reporting any still-playable last-good artifact. Default `matches()` remains
metadata-only so Play does not depend on an edited candidate matching last-good.

An arbitrary `--checkout` is **not** silently registered, copied or promoted into
`RepositoryCache`. For full supervisor/gallery staging put the candidate at
`<cache>/games/<id>`, as above, then run `main.py` with the same `--manifest`,
`--cache`, `--runtime-cache`, `--game-data-root` and `--no-sync`. For an external
checkout, use the returned `PreparedGame`/command/environment directly. The
coordinator alone publishes and promotes approved candidates.

`PreparedGame` carries `entry` (the **actual prepared contract**, possibly
last-good), `checkout` (the launch cwd), `executable`, `data_dir`, and optional
Python `environment`. `child_environment()` returns a fresh environment with:

- `ARCADE_MODE=1`
- absolute, stable per-game `ARCADE_GAME_DATA_DIR`
- **Godot only:** `APPDATA` on Windows or `XDG_DATA_HOME` on Linux equals
  `ARCADE_GAME_DATA_DIR`. Godot's normal `user://` directory therefore lives
  beneath that game's durable directory, including for an unmodified PCK.
- isolated Python: `VIRTUAL_ENV`, its executable directory first on `PATH`,
  `PYTHONNOUSERSITE=1`, and no inherited `PYTHONHOME`/`PYTHONPATH`.

`GameRunner.run(command, cwd, *, game_id, env=None)` receives that environment.
The real runner creates the data directory before spawning. Existing games may
ignore the new variables; new adapters should retain a standalone fallback.
No global environment is mutated, and Python games retain their inherited
APPDATA/XDG roots. Godot games **do not need to rewrite `user://` save paths**:
for default project settings the engine appends
`Godot/app_userdata/<project-name>` (Windows) or
`godot/app_userdata/<project-name>` (Linux). Custom user-directory project
settings keep Godot's normal relative layout beneath the redirected root.
In particular, compiled `user://high_score.save` writes stay per-game and
survive prepared snapshot replacement/rollback. Preparation imports and probes
redirect the same OS variables into separate disposable directories, never the
durable save root. Existing saves outside this new root require a deliberate,
game-specific migration by the operator; the launcher does not overwrite or
silently move them.

Only legacy Python without requirements may omit `executable` when constructing
a command. Runtime resolution never looks for Godot on PATH. Godot argv is:

```text
<verified engine> --path <prepared root> --main-pack <absolute pack path>
  [--script <absolute contained bootstrap>] --rendering-method gl_compatibility --fullscreen
```

For source projects, `--path` points at the directory containing
`project.godot`, and `--main-pack` / `--script` are absent. No `--editor`,
`--import`, pip, download or probe belongs on Play.

## Provisioning and integrity

Authoritative official SHA512 pins are `PINNED_RUNTIMES` in
[`launcher/runtimes.py`](../../launcher/runtimes.py), one for each of:

- 4.4.1-stable / Linux x86_64
- 4.4.1-stable / Windows x86_64
- 4.5.2-stable / Linux x86_64
- 4.5.2-stable / Windows x86_64

URLs are fixed:

```text
https://github.com/godotengine/godot/releases/download/<tag>/Godot_v<tag>_linux.x86_64.zip
https://github.com/godotengine/godot/releases/download/<tag>/Godot_v<tag>_win64.exe.zip
```

The archive is verified **before** extraction. Every member is checked for
containment, portable naming, duplicate/case aliases, normalized names, links/
special files, encryption and extraction limits. Only the exact standard
executable is extracted; the Windows console wrapper is not launched.

The verified ZIP is retained. On a new process, readiness hashes it against the
official pin and independently derives the expected executable hash from its
trusted member. Disk verification uses memoized hashes only while file identity,
size and timestamps are unchanged; Windows additionally reads NTFS ChangeTime
(its `st_ctime` is creation time) to detect preserved-mtime corruption.

Limits: 256 MiB download, 1 GiB total expanded size, 64 members, 20-second socket
read timeout and five-minute total download deadline (plus a pending bounded
read). Imports default to 180 seconds; initial game probes to 45 seconds;
pip install to 300 seconds with zero retries/15-second pip network timeout.
Version probes are bounded to 20 seconds. Expected failures have typed errors.

Installation is staged, checksum/version-probed, then renamed into its pinned
version/platform directory. A failed new version never replaces an older one.
Explicit preparation can repair a missing/corrupt executable from its retained
verified archive without network; `verify()` never repairs or runs anything.

## Preparation, promotion, rollback and saves

1. One worker requests each enabled game at most once per launcher startup.
2. Git fetch touches only objects and a private candidate ref, not source files.
3. A changed revision is fetched **locally** into an independent staging repo.
   No hard-linked/shared object store, in-place reset-hard or clean is used.
4. Edits/untracked work block promotion. Ignored files are copied; a tracked
   candidate collision with ignored data blocks the update. Files are checked
   again after preparation to catch changes made while it ran.
5. Python environments and native snapshots become reusable only after a
   complete successful inventory/receipt. Native import/probe userdata and logs
   use temporary isolated locations. Source projects are never imported in place.
6. The successful `.git/arcade-launcher-prepared.json` moves with its checkout.
   It records launch metadata and prepared artifact/dependency keys. Failed
   preparation cannot replace that receipt or the working source.
7. Promotion retains the whole old checkout in `<cache>/rollback/<id>-<nonce>`.
   A small journal permits disk-only last-good resolution if a directory swap
   was interrupted; subsequent explicit preparation/startup recovers it.
8. Operator rollback copies a retained checkout, preserves **current** ignored
   saves (not stale backup saves), and verifies the old prepared artifact/runtime
   before swapping. Persistent `ARCADE_GAME_DATA_DIR` is never copied or deleted.

For example, after selecting an actual retained rollback directory:

```text
python -m tools.prepare_games --manifest <staged-manifest> --cache <staged-cache>
  --game-data-root <staged-saves> --game <id> --rollback <cache/rollback/id-nonce>
```

Use `--no-sync` or a deliberately pinned manifest ref to keep a rollback from
being updated again at the next online startup. A rollback tool success is not
permission to activate an untested entry.

No automatic garbage collection deletes rollback copies, invalid-version
quarantines, retained engine ZIPs or userdata. Budget disk for the new candidate
**and** last-good; coordinator cleanup must retain wanted saves and rollback
artifacts. Run one writer per cache: checkout guards serialize the UI/worker
within a launcher instance, not independent operator processes. Stage elsewhere
instead of preparing against a running live cache.

## Process ownership and acceptance limits

Games, git and preparation helpers share the same ownership implementation:
POSIX session/process groups with bounded TERM/KILL escalation; Windows suspended
creation, Job Object assignment before execution, and kill-on-close. Cleanup
runs after clean exit, crash, cancellation and timeouts. It does not kill by
executable name or touch unrelated processes.

Games are trusted club code, not sandboxed hostile programs. A POSIX helper must
not deliberately call `setsid()`/daemonize to escape its group. SIGKILL/power loss
cannot run Python cleanup on POSIX. Source/runtime receipts are operational
integrity checks, not a sandbox against a hostile account that owns the cache.

Separate acceptance still requires native Linux display/frame pacing, both
actual controllers where applicable, complete back/retry/gallery-return flows,
offline launch and real save migration checks. Windows version/headless/source/
pack probes and fake-network unit tests do not substitute for that gate.
