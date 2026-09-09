<p align="center">
  <img src="assets/branding/gdc-cmu-logo.png" alt="Game Dev Club — Carnegie Mellon University in Qatar" width="140">
</p>

<h1 align="center">GDC Arcade Launcher</h1>

<p align="center">
  The front door to the Game Dev Club arcade cabinet at Carnegie Mellon University in Qatar.
</p>

---

## What it is

`ArcadeLauncher` is the gallery that greets whoever walks up to the CMU-Q arcade
cabinet. It shows the club's games as a curated wall of cards, lets a visitor
browse them with the joystick alone, and starts the one they pick.

It is a **launcher**, not a game. It owns no gameplay. Its whole job is to look
like it belongs on an arcade machine, keep the club's catalogue current, and get
out of the way the moment somebody presses a button — then be there again when
they come back.

Three things make it more than a menu:

- **A supervisor, not a wrapper.** The launcher completely releases the display,
  the audio device and every joystick before a game starts, then runs the game as
  a separate process. Games do not have to know the launcher exists, and a game
  that crashes cannot take the cabinet down with it — the gallery simply comes
  back with an explanation.
- **Three real view modes.** Grid, Carousel and Cover Flow are three genuinely
  different compositions of the same catalogue, not one layout with the spacing
  changed. Press `Select` to cycle them live.
- **It survives a dead network.** The cabinet's Wi-Fi at a club fair is a rumour,
  not a fact. Games are cached on disk, updates happen in the background, and a
  failed update never blocks play — it just labels the card honestly.

## Architecture

The codebase is layered, and the layering is enforced by a test
(`tests/test_repo_hygiene.py::LayeringTests`).

```
main.py                    arcade entrypoint: parse args, wire everything, exit cleanly
│
├── launcher/              PURE LOGIC — none of these import pygame
│   ├── paths.py           every filesystem location, resolved once
│   ├── errors.py          the exception vocabulary
│   ├── manifest.py        parse + validate data/games.json; path containment rules
│   ├── settings.py        config/launcher.json + ARCADE_LAUNCHER_* env overrides
│   ├── viewmodes.py       the ViewMode enum and its cycle order
│   ├── status.py          GameState / GameStatus / Notice — what a card says
│   ├── cache.py           staged git candidates, promotion, last-good and rollback
│   ├── runtimes.py        pinned/checksummed user-local native Godot engines
│   ├── preparation.py     isolated Python deps, native imports, readiness receipts
│   ├── integrity.py       contained, checksummed, long-path-safe file verification
│   ├── commands.py        direct Python/Godot argv construction, no shell
│   ├── processes.py       owned process groups / Windows kill-on-close jobs
│   ├── sync.py            once-per-startup updates/preparation on a worker thread
│   ├── controls.py        the arcade button map (b=0 … p1=5 … Start=9)
│   ├── input_state.py     axis deadzone, debounce, auto-repeat
│   ├── attract.py         the idle-triggered attract-mode state machine
│   ├── previews.py        attract preview manifest schema + path containment
│   └── supervisor.py      the outer loop: run UI → launch child → run UI → …
│
├── launcher/gallery.py    the Pygame session (the UI half of the supervisor loop)
│
├── launcher/ui/           RENDERING — the only place pygame is imported
│   ├── theme.py           palette, fonts, spacing tokens
│   ├── surfaces.py        an LRU cache so nothing is redrawn needlessly
│   ├── art.py             procedural cover art, seeded per game
│   ├── preview.py         decodes + caches a game's attract preview frames
│   ├── effects.py         glows, gradients, reflections
│   ├── viewmodel.py       GalleryFrame — an immutable description of one frame
│   ├── components.py      shared widgets: header, status badges, banner, toast
│   ├── views/             grid.py · carousel.py · coverflow.py
│   ├── scene.py           picks the view and renders it
│   └── fatal.py           branded on-screen error, for failures before the gallery
│
└── tools/                 generate_previews.py · prepare_games.py (operator staging)
```

### The supervisor loop and the two-level exit

The single most important design decision is that the gallery and a playing
game **never own the display/audio/joysticks at the same time**. The pure-logic
supervisor remains alive while it waits for the child.

```
Supervisor.run()
  ├─▶ GallerySession(state)          SDL up · browse · returns UiOutcome
  │     └─ finally: release SDL      display, audio and joysticks handed back
  ├─▶ ProcessGameRunner(...)         verified Python interpreter or native Godot
  │     └─ waits for the child; captures output; cleans its owned process tree
  └─▶ back to GallerySession(state)  with a notice if the child crashed
```

`StreetFighter/pygame_compat.py` calls `pygame.init()` at import time. If the
launcher still held the display, the child would open onto a surface it does not
own. That is why `GallerySession.__call__` releases SDL in a `finally` block
rather than at the end of the loop body — even a crash inside the gallery gives
the hardware back.

This produces the **two-level exit** a visitor experiences:

| Where you are | Press `P1` | What happens |
| --- | --- | --- |
| Inside a game | The game's tested back/menu action (usually `P1`) | Back one level: pause, menu, then root-menu exit returns to the gallery. |
| At the gallery | `P1` | The launcher exits `0`. You are back at the arcade menu. |

The game owns its back/pause handling; the launcher does not globally translate
`B` into Escape or intercept combat buttons. Godot's normalized joypad indices
must be mapped and tested in-engine, not assumed to equal pygame's raw USB
indices. Gallery bindings below remain unchanged.

## Controls

Everything is reachable from the joystick and buttons. There is no mouse — the
cursor is hidden at start-up, and no interaction requires one.

### On the cabinet

| Input | Button id | Action |
| --- | --- | --- |
| Joystick ← → | axis 0 | Previous / next game |
| Joystick ↑ ↓ | axis 1 | Grid: previous / next row. Carousel and Cover Flow: previous / next game |
| `A` | 1 | **Play** the selected game |
| `Select` | 8 | Cycle view mode: Grid → Carousel → Cover Flow → Grid |
| `P1` | 5 | **Exit** to the arcade menu |
| `B` `X` `Y` `Start`, insert money | 0, 2, 3, 9, 4 | Unbound, deliberately. Three buttons is the whole vocabulary — a visitor should never have to guess. Insert money in particular does nothing: the arcade is free. |

The stick is digital, so each axis is treated as a switch with a `0.5` deadzone.
Holding a direction steps once, pauses `380 ms`, then repeats every `140 ms`; all
three values live in `config/launcher.json`.

Messages clear themselves: an error banner disappears on the next thing you do,
and the "not ready yet" pop fades after about 1.5 seconds. There is nothing to
dismiss and no dialog to get stuck in.

### On a keyboard (development)

| Key | Action |
| --- | --- |
| Arrow keys or `WASD` | Navigate |
| `Enter` or `Space` | Play |
| `Tab` | Next view mode |
| `1` `2` `3` | Jump straight to Grid / Carousel / Cover Flow |
| `Esc` | Exit |

There is no on-screen legend for any of this — the tables above are the
documentation. A visitor only ever needs three buttons and the stick, and a
permanent reminder of that on the screen read as clutter more than it helped.

## Attract mode

Leave the cabinet alone for 30 seconds (`attract_idle_ms`, default `30000`)
and the gallery starts demoing itself: it picks a random view mode, glides
between games the same way a visitor's own stick press would, settles on
one, and plays that game's own short looping preview animation inside its
card — then picks a different view mode and repeats. Only games that are
launchable, currently playable, and actually ship a preview animation are
ever chosen as a target — a coming-soon card, or a launchable game with no
preview yet, would just sit there frozen for the whole dwell period, which
reads as broken rather than as a showcase. If no game qualifies, attract
never engages at all; if exactly one does, attract settles on it once and
stays there rather than cycling view modes with nothing to actually glide
between. It never launches a game, never syncs, and never touches the
network.

**Any input ends it instantly** and puts the gallery back exactly where the
visitor left it — same game selected, same view mode — because the press that
wakes the screen back up is spent purely on that: it is never also treated as
a launch, a view change, or (importantly) an exit, so dismissing attract with
`P1` does not also quit the gallery. The idle clock re-arms the moment attract
is dismissed, so another 30 seconds of silence drops back into it.

The preview animation is supplied by the game, not invented by the launcher:
see [What a game must provide](#what-a-game-must-provide). A game with no
preview keeps showing its ordinary procedural card art, attract or not.

## Setup

Requires **Python 3.10 or newer** and `git` on `PATH`. The cabinet runs Python
3.10, so 3.10 is the floor this is tested against, not just the minimum in
theory.

```bash
git clone https://github.com/GDC-CMU/ArcadeLauncher.git
cd ArcadeLauncher
python -m pip install -r requirements.txt
python main.py
```

Useful flags while developing:

```bash
python main.py --no-sync      # never touch the network; use whatever is cached
python main.py --verbose      # log every cache and subprocess decision
python main.py --cache /tmp/x # put the game checkouts somewhere disposable
python main.py --runtime-cache /tmp/engines --game-data-root /tmp/saves
python main.py --help         # the full list
```

Run the tests and regenerate the screenshots:

```bash
python -m unittest discover -s tests -v
python -m tools.generate_previews
```

The test suite is fully offline and headless — it clones only local fixture
repositories and forces the SDL dummy driver, so it is safe to run anywhere.
Runtime/dependency tests use fake engines/network/pip fixtures; owned-tree tests
spawn real, short-lived Python children. Real Godot/display/controller probes
are separate from the unit suite.

## Deploying to the CMU-Q arcade

The cabinet is an Ubuntu 22.04 x86-64 PC (glibc 2.35, Python 3.10), not an ARM
Raspberry Pi. Its Intel HD 530 supports Godot's Compatibility renderer.
The outer menu uses RetroPie conventions. Games live in
`/home/es/RetroPie/roms/cmu_graphics/<Name>.git`, and **the directory name is
what appears on the outer menu** — so name it the way you want visitors to read
it. The `.git` suffix is part of the convention on that box, alongside
`Tarnival-StreetFighter.git` and `Professor-Invaders.git`. For each entry the
menu pulls the repository, installs `requirements.txt` into a per-game
virtualenv at `<game-dir>/retropie-venv`, then runs `main.py`.

Installing is one clone. This repository is public, so a plain HTTPS clone is
all it takes — no deploy key, no SSH host alias:

```bash
cd /home/es/RetroPie/roms/cmu_graphics
git clone https://github.com/GDC-CMU/ArcadeLauncher.git "Arcade-Launcher.git"
```

The coordinator refreshes the outer menu through the approved deployment
workflow. Runtime preparation does not require rebooting, changing GPU drivers,
installing Godot system-wide, or replacing system Python.

> The deploy-key and SSH-host-alias procedure in the maintainer's instructions
> is only needed for **private** repositories. This one is public.

Only the coordinator publishes/promotes staged, tested revisions and enables
catalog entries. A successful import or headless start is **not** controller/
display/gameplay acceptance. What that means for this repository:

1. **Publish approved launcher support before activating dependent entries.**
   The outer menu pulls `main`; game runtimes/artifacts have their own
   preparation step described below.
2. **Keep `main.py` at the repository root.** The arcade menu invokes it by that
   exact path. Do not rename or move it.
3. **Do not assume a working directory.** It is not documented which directory
   the menu runs `main.py` from, so nothing here resolves a file relative to it
   — every path comes from the package's own location. See
   [Cabinet-specific hazards](#cabinet-specific-hazards).
4. **Keep `requirements.txt` installable.** The menu builds a virtualenv per
   game from it. It pins `pygame-ce`, the maintained fork the cabinet already
   uses.
5. **Exit codes matter.** The launcher returns `0` on a normal exit — that is
   what tells the arcade menu the session ended cleanly. It returns `1` only if
   it could not start at all, and in that case it first paints a readable,
   branded error screen so a club member standing at the cabinet can see what
   went wrong instead of a black rectangle.
6. **Prepare while the network is available.** The startup worker clones missing
   launchable games, provisions pinned engines and prepares dependencies/imports.
   Do this before visitors arrive, or use the separate staging tool. Only fully
   prepared releases are playable offline.

The display is opened at **800×600** with `pygame.SCALED`, so SDL letterboxes the
gallery onto whatever panel is fitted without the layout changing.

### Cabinet-specific hazards

Five failure modes exist only on the cabinet and never on a development machine,
which is exactly what makes them dangerous. All are handled, and each has tests
that fail if the handling is removed.

**The `cmu_graphics` Pygame shim.** The box has `cmu_graphics` installed — the
ROM folder is named after it — and it ships a module that answers to the name
`pygame` without being Pygame. If it wins the import, anything that did a plain
`import pygame` dies before drawing a frame. So nothing here imports Pygame
directly: [`launcher/ui/pygame_runtime.py`](launcher/ui/pygame_runtime.py)
imports it, checks the result really does provide `init`, `display`, `Surface`,
`event`, `font` and the rest, and if it finds a shim it drops **only** that
one `sys.path` entry, clears the module cache and re-imports. If no real Pygame
can be found it raises — it never calls `sys.exit()` — so `main.py` still paints
the branded error screen and still owns the exit code.

**The unknown working directory.** `data/games.json`, `config/launcher.json`,
`assets/branding/gdc-cmu-logo.png` and `.arcade-cache/` are all resolved from
`__file__` in [`launcher/paths.py`](launcher/paths.py), never from
`os.getcwd()`. A single working-directory-relative path would work on every
developer's machine and fail on the cabinet with "manifest not found".
`tests/test_paths.py` proves it by loading everything from an unrelated
directory, in-process and in a fresh interpreter, and refuses to let
`Path.cwd()`, `os.getcwd()` or a relative path literal back into the package.

Games are unaffected by that: a game is still started with **its own checkout**
as the working directory, which is what puts its directory on the child's
`sys.path[0]` so its sibling imports keep resolving.

**Termination has to actually terminate.** The arcade menu stops the launcher by
sending `SIGTERM`, and the cabinet's documented recovery for a process that will
not quit is the physical reset button — which the box's maintainer warns risks
filesystem corruption. The signal handler sets a shutdown flag, but a flag only
read *between* gallery sessions is useless: an idle cabinet never leaves its
session, so the launcher logged the signal and kept running. The gallery loop now
polls that same flag every frame and leaves as though the visitor had pressed
exit, so SDL is released, the sync worker is stopped and any running game is
terminated rather than orphaned. Measured on Linux: **0.05 s** for `SIGTERM` and
`SIGINT`, both exit code 0.

**A joystick is announced twice.** SDL reports a device that is already plugged
in through both the start-up `get_count()` enumeration *and* a `JOYDEVICEADDED`
event, so the cabinet's two sticks produced four "joystick attached" lines. Each
duplicate opened a second SDL handle and replaced the first without closing it.
Devices are now keyed by SDL instance id and re-adding a known one is a no-op
that closes the duplicate handle. Navigation was never affected — held
directions are merged as a set, so a doubled device could not double-step the
selection — and there is a test pinning that, so it stays true.

**No SDL-backed object may outlive the SDL session that created it.** A game
exiting cleanly and the launcher never coming back — with no traceback, no
error, nothing after the child's own "exited with code 0" line — was reported
directly from a cabinet console, and later reproduced exactly: `pygame.error:
Couldn't find glyph` followed by `Windows fatal exception: access violation`,
inside the very first `font.size()` call after the gallery reopened. The
`GallerySession`'s renderer used to be built once, in `__init__`, and reused
for every launch — but `_release_sdl()` runs `pygame.font.quit()` and
`pygame.quit()` between every game, which free every cached `Font` and
`Surface` at the C level. The long-lived renderer kept drawing with the same
Python objects afterwards, now pointing at freed memory: readable as a
missing glyph on the next font lookup, then an access violation on the next
Surface blit — and it took out the *error-notice* banner along with
everything else, since that path draws with the same cached fonts, turning a
recoverable failure into a silent crash. The renderer (and everything it
caches — fonts, surfaces, the logo, decoded preview frames) is now rebuilt
from scratch in `_open_display()` and dropped in `_release_sdl()`, so a stale
reference cannot exist to be drawn with; `tests/test_gallery.py::
RendererLifetimeTests` pins it, including one test that reproduces the exact
access violation above against the pre-fix code.

Separately, and worth keeping regardless: every route out of
`Supervisor.run()` now logs at INFO or louder before it returns or raises,
including the loop's own fall-through and a last-resort handler for anything
that is not even an `Exception` subclass (a stray `SystemExit`), so a genuine
Ctrl+C, a crashed gallery and a silent process death are never
indistinguishable in the log again. `main.py` also enables Python's
`faulthandler` at start-up — it cannot prevent a fatal native fault inside a
C extension such as SDL, but it prints what every thread was doing at the
moment of one, which is what first pointed at the crash above instead of
nothing. A launched game now also runs in its own console process group
(`CREATE_NEW_PROCESS_GROUP` on Windows, `start_new_session` on POSIX) — the
same isolation `launcher/cache.py` already gave git subprocesses — so a
Ctrl+C aimed at the launcher's console and whatever the game's own SDL/input
layer does with a console signal can never be mistaken for one another.

## Offline behaviour

**Yes, the cabinet works with no Wi-Fi.** Fully prepared games keep playing;
the gallery stays interactive. A usable last-good release reads `CACHED OFFLINE`
when an update/preparation fails. A missing or corrupt engine, dependency
environment, entrypoint or prepared artifact makes **that game** `UNAVAILABLE`,
with a reason, rather than crashing the gallery or guessing another runtime.

Game checkouts live in `.arcade-cache/` (git-ignored, never committed). Each
launcher process checks its launchable games **once at startup**, on a background
thread while the gallery remains browsable. Missing games are cloned; installed
games are fetched for changes. Candidates are checked out from a local fetch of
the already downloaded objects, not downloaded from GitHub a second time.

**Playing again does not contact GitHub.** Launching a game and returning to the
gallery reuse the checked local copies and prepared runtime. No download, pip,
Godot import/export, or game probe occurs on Play or gallery return. Disk-only
safety checks still reject a disappeared or modified artifact. Duplicate sync requests are ignored,
including after a failed check. To pick up a newly published build or retry after
Wi-Fi returns, exit and restart the launcher. There is no multi-hour freshness
timer and no new maintenance control: a new process starts a new check.

**Network waits stay bounded.** Each git command has a `network_timeout_s` limit
(`8` seconds by default; see `config/launcher.json`). After a network timeout,
subsequent startup checks fail fast during the runner's short retry cooldown,
letting installed games fall back to `CACHED OFFLINE`. There is no additional
pre-launch network wait once a game's startup check has finished.

Each card reports exactly what is true of it right now, including the short
commit id of the build it is showing — the answer to "am I running the
latest?" without needing a terminal:

| Badge | Meaning |
| --- | --- |
| `PLAYABLE` | Cached and verified by this process's startup check. Press `A` to start it. Its detail line identifies the installed commit. |
| `UPDATING` | Startup fetch/runtime/dependency/import preparation is running. Browsing remains available. |
| `CACHED OFFLINE` | The update failed, but a good checkout is already there. Fully playable, and its detail line still names the cached commit. |
| `UNAVAILABLE` | No usable prepared release (missing/damaged source, runtime, deps or artifact). The detail explains why. |
| `COMING SOON` | Curated in the manifest but not released yet. Not playable. |
| `QUEUED` | Waiting its turn in the sync queue. |

Colour, label *and* — for the busy states — a pulsing dot all encode the same
thing, so the distinction survives a photo, a dim projector or a colour-blind
visitor.

The rules that follow from this:

- **A failed update never replaces a working game.** Fetch changes objects/refs
  only. Preparation runs against a separate candidate. Source plus its
  successful preparation receipt are promoted together only when ready; the
  whole previous checkout is retained for rollback. There is no in-place
  `reset --hard` or `git clean`.
- **Source edits and saves survive.** Tracked edits/untracked work block
  promotion. Ignored files are copied and checked again before promotion;
  a new tracked file colliding with ignored data blocks the update. New games
  store saves in `ARCADE_GAME_DATA_DIR`, outside disposable caches.
- **A ready game launches locally.** No new fetch is requested on Play or on
  gallery return. A checkout guard prevents an update from modifying files
  while that game's child process is running.
- **Coming-soon entries never touch the network.** They structurally carry no
  repository, ref or entrypoint, so there is nothing to clone.
- **No arbitrary manifest commands.** Entrypoints, pack bootstraps and
  requirements must remain contained in their checkout/prepared snapshot.
  Execution uses a verified user-local engine/interpreter and argv lists,
  never a shell, PATH-selected Godot, browser, Wine or console wrapper.
- **`--no-sync` is a hard promise.** In offline mode `git` is not invoked at all:
  startup and pre-launch readiness checks verify only what is already on disk.

### Native engines, preparation and persistent data

See [the runtime/operator contract](docs/native-runtime/interfaces.md) for the
exact schema, APIs, checksums, preparation limits and rollback commands.

| Location | Linux default | Windows default |
| --- | --- | --- |
| Source/staging/rollback/prepared artifacts | `<launcher>/.arcade-cache/` | `<launcher>\.arcade-cache\` |
| Godot engines | `$XDG_CACHE_HOME/arcade-launcher/runtimes` (`~/.cache` if unset) | `%LOCALAPPDATA%\GDC-CMU\ArcadeLauncher\runtimes` |
| Persistent game data | `$XDG_DATA_HOME/arcade-launcher/games/<id>` (`~/.local/share` if unset) | `%LOCALAPPDATA%\GDC-CMU\ArcadeLauncher\userdata\<id>` |

`ARCADE_LAUNCHER_RUNTIME_CACHE` and `ARCADE_LAUNCHER_DATA_ROOT` can override
the last two roots with absolute paths. Data roots may not overlap disposable
caches. `--runtime-cache` / `--game-data-root` are their CLI equivalents.

Every child receives `ARCADE_MODE=1` and `ARCADE_GAME_DATA_DIR`. For Godot only,
the child's `APPDATA` (Windows) or `XDG_DATA_HOME` (Linux) is also redirected
to that per-game directory, so even compiled `user://high_score.save` paths
remain durable without changing the pack. Godot appends its normal
project-specific subdirectory. Preparation probes use disposable userdata
elsewhere. The launcher's environment and legacy Python OS save locations are
unchanged; migrating pre-existing Godot saves is an explicit operator step.

Only standard **Godot 4.4.1-stable and 4.5.2-stable**, Linux/Windows x86_64,
are supported. The official platform ZIP is SHA512-verified before extraction,
then the executable is checked against that archive and version-probed. The ZIP
is retained for offline repair and independent verification on later startups.
No Mono builds, export templates or unrelated platforms are downloaded.

Source `project.godot` entries are imported into a versioned snapshot and run
directly with the native engine. Exported `.pck` entries use `--main-pack`.
Both use `--rendering-method gl_compatibility`. Windows file verification handles
long imported paths without requiring a registry change.

New Python adapters declare `python_requirements`. The service fingerprints
requirements (including contained `-r`/`-c` includes) plus interpreter ABI,
prepares a `venv --copies` without system site packages, installs there, runs
`pip check`, and inventories installed files. It never upgrades the launcher's
pygame installation. The existing three Python games may omit this field and
keep the current interpreter.

Operator examples — use **separate staged paths and a staged manifest** while
validating candidates; do not activate a shipped disabled entry just to test it:

```bash
python -m tools.prepare_games --manifest /staging/games.json --cache /staging/cache --game-data-root /staging/saves
python -m tools.prepare_games --manifest /staging/games.json --cache /staging/cache --game-data-root /staging/saves --verify-only
python -m tools.prepare_games --manifest /staging/games.json --cache /staging/cache --game-data-root /staging/saves --offline
python -m tools.prepare_games --manifest /staging/games.json --game flappy-scotty --checkout "/staging/cache/games/flappy-scotty" --cache /staging/cache --game-data-root /staging/saves --offline
```

The first prepares once; `--verify-only` performs no subprocesses/writes/network;
`--offline` explicitly permits local engine repair/import preparation but no
downloads. A failed candidate returns a nonzero tool exit code even when
last-good is still playable. Ordinary `main.py --no-sync` never prepares.

`--checkout` prepares one explicit staged checkout's current dirty/untracked
files without any git sync or source promotion. It requires an enabled entry
in the **staged** manifest, one `--game`, and explicit cache/data roots.
Godot snapshots are keyed by actual content rather than Git HEAD; operator
verification detects candidate edits even when last-good is still playable.
To exercise the gallery, use `<cache>/games/<id>` as that staged checkout and
run `main.py --no-sync` with the same roots/manifest. An arbitrary external
checkout is not automatically installed into the managed cache. See the
[API/staging examples](docs/native-runtime/interfaces.md#explicit-candidate-staging-including-dirty-sources).

Children, git helpers and preparation commands have owned lifetimes. POSIX uses
a separate session/process group with terminate/kill escalation; Windows starts
suspended, joins a kill-on-close Job Object, then resumes. Helpers are cleaned
after normal exit, crash, timeout and cancellation, not just parent-PID kills.

## Screenshots

Three deliberately different compositions of the same eight games, under one
identical header. All are rendered from the real view code by `python -m
tools.generate_previews`, at the cabinet's exact 800×600, and a test fails if
they drift out of date.

The header -- logo, wordmark, subtitle and the mode chip -- is drawn by a
single shared component and never changes as you cycle views: same wording,
same size, same position. Everything that makes the three modes look
different lives in the content area below it.

They show the shipped `data/games.json` exactly as a healthy cabinet would --
eight accepted games, including two native Godot games. Availability lives entirely on the
per-card badge; nothing here is a mock-up.

`docs/screenshots/render-manifest.json` records the SHA-256 of every PNG, a
fingerprint of the code and data that produced them, and the Pygame/SDL_ttf
build that rasterised them. `tests/test_previews.py` recomputes the fingerprint
and tells you to regenerate if the UI changed — that check is exact and runs
everywhere. The stricter pixel-for-pixel comparison is skipped, with a message
saying so, when your Pygame differs from the recorded one: different SDL_ttf
builds antialias the same bundled font differently, so identical UI produces
different bytes. Regenerate on any machine; the fingerprint is what has to
match, not the pixels.

### Grid — *see everything at once*

A board of equal cards, always three columns -- four would be unreadable at
800x600 -- sized to the rows actually needed: 1-3 games get one big row, 4-6
get two, and 7 or more get the permanent maximum of three. Past nine games the
board scrolls vertically instead of growing a fourth row or paginating:
pressing down past the last visible row eases the view down by exactly enough
to keep the selection in sight, with a restrained sliver of a scrollbar as the
only hint that there is more below. Focus is carried by a raised card, a
bright rule and a glow rather than by size.

![Grid view](docs/screenshots/grid.png)

### Carousel — *one game, properly introduced*

A single hero card centre stage, flanked by dimmed neighbours that glide in and
out as the selection changes, over a full-width description panel. Position
dots mark the selected game's place in the row.

![Carousel view](docs/screenshots/carousel.png)

### Cover Flow — *the arcade showpiece*

A pseudo-3D shelf: cards recede in perspective with depth-scaled dimming, sitting
on a reflective floor under a horizon glow. The selected title sits below the
shelf where nothing overlaps it.

![Cover Flow view](docs/screenshots/cover-flow.png)

### Status badges — *reference sheet*

Not a gallery screenshot. With the current manifest an honest frame can only ever
contain `PLAYABLE` and `COMING SOON`, because the other four states are reached by
syncing and a coming-soon entry is never synced. Rather than invent states for real
club games, the full vocabulary is shown here — drawn by the same
`draw_status_badge` the cards use, so the two can never drift apart — alongside the
banner the supervisor raises when a game exits badly.

![Status badge reference](docs/screenshots/status-badges.png)

## Changing the look

Restyling the launcher does not mean hunting through three view files — the
shared pieces each live in exactly one place:

- **Colours** — the single named palette is `PALETTE` in
  `launcher/ui/theme.py`. Every view, badge and effect pulls its colours from
  there by name (`PALETTE["cmu_red"]`, `PALETTE["electric_cyan"]`, ...), so
  changing an entry once changes it everywhere it is used, consistently
  across all three modes.
- **Backdrop** — the dark gradient field behind every view is
  `Renderer.background()` in `launcher/ui/scene.py`. It is built once per
  screen size and cached, so a change there is still cheap.
- **Header / marquee** — the logo, wordmark, subtitle and mode chip are
  `draw_gallery_header()` in `launcher/ui/components.py`. It is deliberately
  the *only* place that draws the header: all three views call it as-is
  rather than composing their own, which is what keeps it identical no
  matter which view is on screen. Change it there and every view picks it up.
- **Card art** — the procedural, seeded-per-game cover art is
  `render_card_art()` in `launcher/ui/art.py`, driven entirely by each game's
  `art` block in `data/games.json` (`motif`, three `palette` names, and a
  `seed`). Adding a new look means adding a motif function there and picking
  it by name in the manifest — no image asset to draw or ship.

## Changing the default gallery mode

The mode shown when the cabinet boots is `default_view` in
`config/launcher.json`:

```json
{
  "default_view": "carousel"
}
```

Valid values are `grid`, `carousel` and `cover-flow`. To try one without editing
the file:

```bash
ARCADE_LAUNCHER_VIEW=cover-flow python main.py     # Linux / macOS
$env:ARCADE_LAUNCHER_VIEW='cover-flow'; python main.py   # PowerShell
```

The same file also holds `fullscreen`, `frame_rate`, `sync_on_start`,
`nav_initial_delay_ms`, `nav_repeat_ms`, `axis_deadzone`, `network_timeout_s`
and `attract_idle_ms`. An invalid value is reported and replaced with the
default rather than crashing the cabinet.

## Adding a game

The gallery is **curated**: it shows exactly what `data/games.json` lists, in that
order. Nothing is discovered automatically, because an arcade at a club fair is
the wrong place to find out that somebody's work-in-progress does not start.

Prepare and test new entries in a separate manifest first. Flappy's accepted
original-pack adapter illustrates the native runtime fields:

```json
{
  "id": "flappy-scotty",
  "title": "Flappy Scotty",
  "description": "Navigate through tricky obstacles and protect Scotty.",
  "runtime": "godot",
  "godot_version": "4.4.1-stable",
  "launchable": true,
  "repository": "https://github.com/GDC-CMU/FlappyScotty.git",
  "ref": "main",
  "entrypoint": "Flappy Scotty.pck",
  "startup_script": "arcade/bootstrap.gd",
  "art": { "motif": "flight", "palette": ["electric_cyan", "warm_amber", "ink"], "seed": 3303 }
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `id` | yes | Lowercase, `a–z 0–9 -`. Also the cache directory name. |
| `title` | yes | Shown on the card. |
| `description` | yes | One or two sentences; the views wrap it for you. |
| `runtime` | yes | `python` or `godot`. Unknown values are rejected. |
| `launchable` | yes | `false` renders a `COMING SOON` card and nothing is cloned. |
| `repository` | if launchable | Credential-free `https://`, no query/fragment. |
| `ref` | if launchable | Branch, tag, or full 40-character commit SHA. |
| `entrypoint` | if launchable | Contained relative Python script, `.pck`, or `project.godot`. Spaces are supported. |
| `godot_version` | Godot only | Exactly `4.4.1-stable` or `4.5.2-stable`. |
| `startup_script` | optional, Godot pack only | Contained relative `.gd` bootstrap, passed as one `--script` argument. |
| `python_requirements` | new Python adapters | Contained relative requirements file for an isolated environment; may be empty for stdlib-only games. |
| `note` | no | Small print under the description. |
| `art` | yes | `motif`, a three-colour `palette` and a `seed` for the generated cover. |

Only after the native/controller/offline/save/return-to-gallery gate passes
does the coordinator fill in delivery metadata and set `launchable` to `true`.
Disabled entries carry no delivery/preparation metadata and are never fetched.
The current shipped catalog enables all eight accepted club games. After a catalog change:

```bash
python -m unittest discover -s tests -v    # validates the shipped manifest
python -m tools.generate_previews          # refresh the screenshots
```

The manifest is validated at start-up. A malformed entry is a hard failure with a
named field and a readable message — the cabinet tells you which game is wrong.

## What a game must provide

To be launchable from this gallery, a game must:

1. **Be a public repository under `https://`.** Cloned shallow and pinned to `ref`.
2. **Declare its runtime and one contained entrypoint.** Python runs with its
   prepared interpreter and checkout cwd; legacy Python retains `sys.executable`.
   Godot runs directly from its prepared snapshot, not an orphan-prone wrapper.
3. **Own the display while playing.** The gallery has fully released SDL before
   the child starts, and rebuilds its renderer/fonts after the child finishes.
4. **Provide a complete controller lifecycle.** Start, real play, pause/back,
   retry and root-menu exit must work without mouse, keyboard or terminal.
   A Python `sys.exit(0)` or Godot `get_tree().quit()` at the root returns to the
   gallery. Back from gameplay may first pause or return to an in-game menu.
5. **Exit eventually.** The launcher waits for the child. A game that never exits
   holds the cabinet.
6. **Not require the network at runtime.** The fair's Wi-Fi will not be there.
7. **Use `ARCADE_GAME_DATA_DIR` for new arcade saves.** Every child also receives
   `ARCADE_MODE=1`. Keep a standalone fallback when these variables are absent.
   Do not put new persistent data in a venv, import directory or disposable build.
8. **Keep helpers in the owned process group/job.** Do not daemonize, detach a
   POSIX session or spawn a background server that escapes the supervisor.

No import of the launcher, no shared globals, no subclassing. Games stay
standalone programs — `StreetFighter` runs from this gallery **unmodified**.

Optionally, a game may also ship an **attract preview**: a short, pre-rendered
looping animation the gallery plays inside its card during attract mode (see
[Attract mode](#attract-mode)). It lives at a fixed location in the game's own
checkout:

```
assets/preview/manifest.json
assets/preview/frame_000.png
assets/preview/frame_001.png
...
```

```json
{
  "version": 1,
  "fps": 8,
  "frames": ["frame_000.png", "frame_001.png", "frame_002.png"]
}
```

- Entirely optional — a game without `assets/preview/` is not an error, and
  a coming-soon game (which has no checkout at all) never has one.
- Author frames small, at the card's aspect ratio (roughly 160×120–200×150)
  and keep the loop short (1–3 seconds). `fps` must be an integer, 1–30.
- The launcher never trusts this: every frame path is proven to stay inside
  `assets/preview/` in that game's own checkout (the same containment rule
  applied to `entrypoint`), and hard caps bound frame count, per-frame pixel
  dimensions and total decoded bytes. Anything missing, malformed, unreadable
  or over a cap is a single logged warning and a silent fallback to the
  game's ordinary procedural card art — never a crash, never a blank card.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Branded error screen at start-up | The manifest or config file is invalid, or unreadable | The screen names the file and the field. Fix it and re-run. |
| A card shows `UNAVAILABLE` | Source, runtime, dependencies or artifact are missing/damaged | Read its detail; run explicit staged preparation. Play never downloads a missing engine. |
| Every card shows `CACHED OFFLINE` | No network, but the cache is good | Nothing to do — cached games still play. |
| `COMING SOON` on a released game | Activation has not been approved/published | Coordinator completes the cabinet gate before enabling the entry. |
| `CACHED OFFLINE` mentions local edits or an ignored-data collision | An update would discard work or overwrite user data | Preserve/review that work separately; the launcher will not reset/clean it. |
| Runtime ZIP or executable checksum failure | Damaged/incomplete native installation | Explicit preparation can repair from the verified retained ZIP offline, or redownload the pinned archive online. |
| "That game isn't ready yet" toast | You pressed Play on a non-playable card | Expected. The launcher refuses rather than failing halfway. |
| Game starts, then the gallery reappears with a banner | The child exited non-zero | The banner shows the exit code; `--verbose` logs the child's output. |
| Joystick does nothing | It was plugged in after start-up | Hot-plug is handled; if not, restart the launcher. |
| Nothing renders / black screen off-cabinet | No display available | `SDL_VIDEODRIVER=dummy` for headless runs, or use the preview tool. |
| Black screen on the cabinet, launcher never appears | `cmu_graphics` ships a Pygame shim that can win the import and shadow the real module | Already handled: `launcher/ui/pygame_runtime.py` detects the shim, drops only its `sys.path` entry and re-imports. If it still fails you get a branded screen, not a black one — `python main.py --verbose` names what loaded. |
| "manifest not found" on the cabinet only | Something resolved a repository file relative to the working directory, which the arcade menu does not guarantee | Already handled: every path derives from `launcher/paths.py`. If this reappears, `python -m unittest discover -s tests -v` will point at the offending file. |
| `pygame.error: No available video device` | Headless shell | Same as above. |
| Screenshot tests fail right after cloning | Your Pygame/SDL_ttf differs from the one that generated the committed PNGs | Not expected any more — that comparison now skips itself with an explanatory message. If a screenshot test *does* fail it means the UI really did change: run `python -m tools.generate_previews` and commit the result. |
| Launcher will not close; the menu cannot stop it | A termination signal was caught but the running gallery never saw it | Fixed: the loop checks the shutdown flag every frame and exits in ~50 ms. Never use the reset button for this — the maintainer warns it risks filesystem corruption. |
| Each joystick logged twice at start-up | SDL announces an already-connected device through both enumeration and `JOYDEVICEADDED` | Fixed: devices are keyed by SDL instance id, so the second announcement is ignored. Navigation was never double-stepping. |
| Launcher ends after a game exits, with nothing after "exited with code N" in the log | Fixed: the renderer used to be built once and reused, so it kept drawing with fonts/surfaces `_release_sdl()` had just freed — a stale font lookup, then a native access violation, on the very next session | The renderer is now rebuilt fresh every session and dropped on release; `RendererLifetimeTests` pins it. If a silent ending still recurs, `faulthandler` (enabled in `main.py`) prints the state of every thread at the moment of a fatal fault to the same stderr stream instead of nothing. |

## Club-fair preflight

Ten minutes before the doors open, on the cabinet:

1. `git pull` — the cabinet menu does this, but confirm it succeeded.
2. `python -m pip install -r requirements.txt` — confirm `pygame-ce` is present.
3. `python -m unittest discover -s tests -v` — must be all green.
4. **While you still have network**, run `python main.py` once and wait for every
   card to leave `UPDATING`. This fills the cache for the day.
5. Check the badges: every game you intend to demo reads `PLAYABLE`.
6. Launch each demo, exercise play/pause/retry/back and root-menu exit — confirm
   you land back at the gallery with no lingering helper/window/audio.
7. From the gallery press `P1` — confirm you land back at the arcade menu.
8. Press `Select` three times — confirm all three views render and come back
   around to where you started.
9. Confirm the mode you want visitors to see first is the one that boots
   (`default_view`).
10. **Unplug the network and repeat step 6.** Cached games must still play. This
    is the check that actually saves the day.

---

<p align="center">
  <sub>Built by the Game Dev Club · Carnegie Mellon University in Qatar</sub>
</p>
