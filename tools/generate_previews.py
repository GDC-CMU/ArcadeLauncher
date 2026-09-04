"""Render the launcher's documentation screenshots to ``docs/screenshots/``.

Run from the repository root::

    python -m tools.generate_previews

The tool is fully headless (it forces the SDL dummy video driver), needs no
network, and pins every animated value, so the same commit always produces the
same 800x600 PNGs.  It renders the *real* view code -- these are screenshots of
the launcher, not mock-ups.

Four files are written: one per view mode, plus a clearly-labelled status badge
reference sheet.  The three gallery shots render the shipped manifest exactly as
a healthy cabinet would show it, so their header reads the true tally.  The
sheet exists because that honest frame cannot contain every badge at once, and
inventing states for real club games would make the screenshots lie.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

# The dummy driver must be selected before pygame initialises the display.
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

if __package__ in (None, ""):  # pragma: no cover - direct-script convenience
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Pygame comes from launcher.ui.pygame_runtime, never a bare ``import pygame``:
# on the arcade cabinet the name can resolve to the cmu_graphics shim.
from launcher.ui.pygame_runtime import pygame  # noqa: E402

from launcher.manifest import Manifest, load_manifest  # noqa: E402
from launcher.paths import (  # noqa: E402
    BRANDING_LOGO,
    MANIFEST_FILE,
    REPO_ROOT,
    SCREENSHOTS_DIR,
)
from launcher.status import GameState, GameStatus, Notice  # noqa: E402
from launcher.ui import SCREEN_SIZE  # noqa: E402
from launcher.ui.components import (  # noqa: E402
    draw_marquee,
    draw_notice,
    draw_status_badge,
)
from launcher.ui.effects import panel  # noqa: E402
from launcher.ui.scene import Renderer  # noqa: E402
from launcher.ui.theme import PALETTE, mix, shade  # noqa: E402
from launcher.ui.viewmodel import GalleryFrame  # noqa: E402
from launcher.viewmodes import CYCLE_ORDER, ViewMode  # noqa: E402

__all__ = [
    "FILENAMES",
    "GALLERY_FILENAMES",
    "BADGE_SHEET_FILENAME",
    "BADGE_MEANINGS",
    "RENDER_MANIFEST_FILENAME",
    "SOURCE_PATTERNS",
    "cabinet_states",
    "assert_states_are_reachable",
    "build_frame",
    "render_badge_sheet",
    "generate",
    "render_inputs",
    "fingerprint_sources",
    "describe_environment",
    "fingerprint_environment",
    "write_render_manifest",
    "read_render_manifest",
    "main",
]

#: One file per mode, named after the mode rather than numbered.
GALLERY_FILENAMES: dict[ViewMode, str] = {
    ViewMode.GRID: "grid.png",
    ViewMode.CAROUSEL: "carousel.png",
    ViewMode.COVER_FLOW: "cover-flow.png",
}

#: Kept under the old name for callers that only want the gallery shots.
FILENAMES = GALLERY_FILENAMES

#: The status reference sheet.  Explicitly *not* a gallery screenshot -- see
#: :func:`render_badge_sheet` for why it exists.
BADGE_SHEET_FILENAME = "status-badges.png"

#: Frozen animation clock, so the PNGs are byte-stable across runs.
PINNED_TIME_MS = 1_400

#: Which card each shot selects, chosen to show a different game every time.
SELECTION: dict[ViewMode, int] = {
    ViewMode.GRID: 0,
    ViewMode.CAROUSEL: 2,
    ViewMode.COVER_FLOW: 3,
}


def cabinet_states(manifest: Manifest) -> dict[str, GameState]:
    """Build the state map a healthy cabinet actually shows.

    This is deliberately *not* a scripted set of interesting-looking badges.
    An earlier revision assigned ``UPDATING``/``CACHED OFFLINE``/``UNAVAILABLE``
    to coming-soon games to fit the whole status vocabulary into one frame; but
    those states are only reachable by syncing, coming-soon entries are never
    synced, and the resulting header read "2 PLAYABLE  2 SOON" for a manifest
    holding one launchable game and five coming-soon ones.  Documentation that
    shows an impossible screen is worse than documentation that shows a plain
    one, so the screenshots now render exactly what the launcher renders.

    The full badge vocabulary is documented instead by
    :func:`render_badge_sheet`, which is clearly labelled as a reference.
    """
    states = {
        entry.id: (
            GameState(entry.id, GameStatus.READY, "Synced just now")
            if entry.launchable
            else GameState(
                entry.id, GameStatus.COMING_SOON, entry.note or "In development"
            )
        )
        for entry in manifest
    }
    assert_states_are_reachable(manifest, states)
    return states


def assert_states_are_reachable(
    manifest: Manifest, states: dict[str, GameState]
) -> None:
    """Refuse to render a state the launcher itself could never produce.

    Raises:
        ValueError: A coming-soon entry carries a sync-only status.
    """
    for entry in manifest:
        status = states[entry.id].status
        if not entry.launchable and status.requires_sync:
            raise ValueError(
                f"'{entry.id}' is coming-soon, so it can never reach "
                f"{status.name}; the launcher never syncs it. Rendering that "
                f"would document a screen the product cannot show."
            )


def build_frame(manifest: Manifest, mode: ViewMode) -> GalleryFrame:
    """Assemble the deterministic frame captured for *mode*."""
    return GalleryFrame.build(
        manifest,
        cabinet_states(manifest),
        selected_index=SELECTION[mode],
        view_mode=mode,
        time_ms=PINNED_TIME_MS,
        focus_ms=400,
        scroll=float(SELECTION[mode]),
    )


#: What each badge means, in the order a reader meets them on the cabinet.
BADGE_MEANINGS: tuple[tuple[GameStatus, str], ...] = (
    (GameStatus.READY, "Cached and current. Press A to start it."),
    (GameStatus.UPDATING, "A background fetch is running. Play waits for it."),
    (GameStatus.PENDING, "Waiting its turn in the sync queue."),
    (GameStatus.CACHED_OFFLINE, "Update failed, cached copy is good. Playable."),
    (GameStatus.UNAVAILABLE, "Never downloaded here. Not playable."),
    (GameStatus.COMING_SOON, "Curated but not released yet. Never synced."),
)

#: The banner the supervisor raises when a game exits non-zero.
CRASH_EXAMPLE = Notice(
    "error",
    "STREET FIGHTER CLOSED UNEXPECTEDLY",
    "Exit code 1 - back at the gallery, pick another game or try again.",
)


def render_badge_sheet(renderer: Renderer, size: tuple[int, int]) -> pygame.Surface:
    """Render the status reference sheet.

    Criterion D6 asks that Playable, Coming Soon, Updating and Unavailable stay
    visually distinct.  With the shipped manifest -- one launchable game and
    five coming-soon ones -- no single honest gallery frame can contain all of
    them, because five of the six cards can only ever read ``COMING SOON``.

    Rather than relabel a real club game to fill the gap, the vocabulary is
    documented here, on a sheet that says plainly what it is.  Every pill is
    drawn by :func:`~launcher.ui.components.draw_status_badge`, the same
    function the cards use, so the colours and labels cannot drift from the
    gallery.
    """
    ctx = renderer.ctx
    surface = pygame.Surface(size)
    surface.blit(renderer.background(size), (0, 0))

    draw_marquee(surface, ctx, pygame.Rect(24, 0, size[0] - 48, 74))
    ctx.pixel.draw(
        surface,
        "STATUS REFERENCE",
        (size[0] - 26, 37),
        2,
        PALETTE["electric_cyan"],
        anchor="midright",
    )

    strip = pygame.Rect(24, 84, size[0] - 48, 26)
    panel(surface, strip, shade(PALETTE["panel"], 0.7), PALETTE["deep_cyan"], radius=4)
    caption = ctx.fonts.render(
        "Reference sheet - not a gallery screenshot. Every pill below is drawn by the "
        "code the cards use.",
        "caption",
        PALETTE["steel"],
    )
    surface.blit(caption, caption.get_rect(midleft=(strip.left + 12, strip.centery)))

    table = pygame.Rect(24, 120, size[0] - 48, 322)
    panel(surface, table, shade(PALETTE["panel"], 0.85), PALETTE["deep_cyan"], radius=6)

    row_height = (table.height - 24) // len(BADGE_MEANINGS)
    for index, (status, meaning) in enumerate(BADGE_MEANINGS):
        centre_y = table.top + 12 + row_height * index + row_height // 2
        if index:
            pygame.draw.line(
                surface,
                mix(PALETTE["panel"], PALETTE["steel"], 0.25),
                (table.left + 18, centre_y - row_height // 2),
                (table.right - 18, centre_y - row_height // 2),
            )
        draw_status_badge(
            surface,
            ctx,
            (table.left + 22, centre_y),
            status,
            align="midleft",
            scale=2,
            time_ms=PINNED_TIME_MS,
        )
        ctx.pixel.draw(
            surface,
            "PLAYS" if status.is_playable else "LOCKED",
            (table.left + 236, centre_y),
            1,
            PALETTE["mint"] if status.is_playable else PALETTE["steel"],
            anchor="midleft",
        )
        meaning_text = ctx.fonts.render(meaning, "body", PALETTE["bone"])
        surface.blit(
            meaning_text, meaning_text.get_rect(midleft=(table.left + 316, centre_y))
        )

    ctx.pixel.draw(
        surface,
        "WHEN A GAME EXITS BADLY",
        (26, 464),
        2,
        PALETTE["bone"],
        anchor="midleft",
    )
    draw_notice(surface, ctx, pygame.Rect(24, 482, size[0] - 48, 62), CRASH_EXAMPLE)
    footer = ctx.fonts.render(
        "The gallery always comes back - a crashed game can never take the cabinet down.",
        "caption",
        PALETTE["steel"],
    )
    surface.blit(footer, footer.get_rect(midleft=(26, 566)))

    vig, lines = renderer.overlays(size)
    surface.blit(vig, (0, 0))
    surface.blit(lines, (0, 0))
    return surface


def generate(output_dir: Path, manifest_path: Path) -> list[Path]:
    """Render every mode plus the status sheet, and return the files written."""
    manifest = load_manifest(manifest_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    pygame.display.init()
    pygame.font.init()
    try:
        # A real display surface makes convert_alpha() available, which is what
        # the cabinet does; the dummy driver keeps it off-screen.
        pygame.display.set_mode(SCREEN_SIZE)
        renderer = Renderer()
        written: list[Path] = []
        for mode in CYCLE_ORDER:
            surface = renderer.render(build_frame(manifest, mode), SCREEN_SIZE)
            destination = output_dir / GALLERY_FILENAMES[mode]
            pygame.image.save(surface, str(destination))
            written.append(destination)

        sheet = output_dir / BADGE_SHEET_FILENAME
        pygame.image.save(render_badge_sheet(renderer, SCREEN_SIZE), str(sheet))
        written.append(sheet)
        return written
    finally:
        pygame.display.quit()
        pygame.font.quit()
        pygame.quit()


#: Sidecar written next to the PNGs, recording what produced them.  See
#: :func:`write_render_manifest` for why the screenshots need one.
RENDER_MANIFEST_FILENAME = "render-manifest.json"

#: Everything whose contents can change the rendered pixels, relative to the
#: repository root.  Deliberately a superset: it is better to ask for a
#: regeneration that turns out to be a no-op (the PNGs come back byte-identical
#: and only the sidecar's fingerprint changes) than to miss a real UI edit.
SOURCE_PATTERNS: tuple[str, ...] = (
    "tools/generate_previews.py",
    "launcher/ui/**/*.py",
    "launcher/status.py",
    "launcher/viewmodes.py",
    "data/games.json",
)

#: Binary render inputs, hashed byte-for-byte.
BINARY_INPUTS: tuple[Path, ...] = (BRANDING_LOGO,)

#: Suffixes hashed as text, with line endings normalised.  A Windows checkout
#: with ``core.autocrlf`` on would otherwise disagree with the cabinet about the
#: fingerprint of files that are character-for-character identical.
TEXT_SUFFIXES = (".py", ".json")


def render_inputs() -> list[tuple[str, bytes]]:
    """Every file that determines the screenshots, as ``(path, raw content)``.

    Sorted by POSIX path so the order is identical on Windows and Linux.
    Content is returned exactly as it sits on disk; newline normalisation is
    :func:`fingerprint_sources`' job, so that it applies however the inputs
    were obtained.
    """
    paths: set[Path] = set(BINARY_INPUTS)
    for pattern in SOURCE_PATTERNS:
        paths.update(
            path
            for path in REPO_ROOT.glob(pattern)
            if path.is_file() and "__pycache__" not in path.parts
        )
    return [
        (path.relative_to(REPO_ROOT).as_posix(), path.read_bytes())
        for path in sorted(
            paths, key=lambda item: item.relative_to(REPO_ROOT).as_posix()
        )
    ]


def fingerprint_sources(inputs: list[tuple[str, bytes]] | None = None) -> str:
    """Hash the render inputs into one comparable token.

    Text inputs are newline-normalised first: a Windows checkout with
    ``core.autocrlf`` on would otherwise disagree with the cabinet's LF one
    about files that are character-for-character identical, and the whole point
    of this fingerprint is to give the same answer everywhere.

    Args:
        inputs: The ``(path, content)`` pairs to hash. Defaults to
            :func:`render_inputs`. Injectable so a test can prove the
            fingerprint actually reacts to a change instead of quietly
            becoming a constant.
    """
    digest = hashlib.sha256()
    for name, content in render_inputs() if inputs is None else inputs:
        if name.endswith(TEXT_SUFFIXES):
            content = content.replace(b"\r\n", b"\n")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def describe_environment() -> dict[str, object]:
    """What about this machine decides how text is rasterised.

    Not the Python version and not the operating system -- neither changes a
    pixel.  What does change pixels is the glyph rasteriser: the cabinet runs
    pygame-ce 2.5.6 against SDL_ttf 2.24.0 while this was generated on
    pygame 2.5.2 against SDL_ttf 2.20.1, and those two FreeType builds
    antialias the *same font file* differently.  (The font itself is bundled
    with Pygame and is byte-identical on both, which is why it is recorded
    here: if it ever stops being identical, that shows up as a mismatch too.)
    """
    pygame.font.init()
    font_name = pygame.font.get_default_font()
    font_path = Path(pygame.font.__file__).parent / font_name
    font_digest = (
        hashlib.sha256(font_path.read_bytes()).hexdigest()
        if font_path.is_file()
        else "unknown"
    )
    ttf_version = getattr(pygame.font, "get_sdl_ttf_version", None)
    return {
        "pygame": pygame.version.ver,
        "pygame_flavour": "pygame-ce" if getattr(pygame, "IS_CE", None) else "pygame",
        "sdl": list(pygame.version.SDL),
        "sdl_ttf": list(ttf_version()) if ttf_version else None,
        "default_font": font_name,
        "default_font_sha256": font_digest,
    }


def fingerprint_environment(environment: dict[str, object] | None = None) -> str:
    """Hash a rendering environment description into a comparable token."""
    described = describe_environment() if environment is None else environment
    payload = json.dumps(
        {key: described.get(key) for key in sorted(described) if key != "fingerprint"},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_render_manifest(output_dir: Path, written: list[Path]) -> Path:
    """Record what produced the screenshots, next to the screenshots.

    Byte-comparing a committed PNG against a fresh render is the natural way to
    prove the screenshots are current, and it is exactly right *on the machine
    that generated them*.  It is also unrunnable anywhere else: SDL_ttf
    rasterises the bundled font differently between builds, so the cabinet, a
    teammate's laptop and CI all produce visibly identical but bitwise
    different PNGs.  A suite that fails on a correct checkout is worse than no
    suite, and loosening the comparison to a pixel tolerance does not work
    either -- measured against this UI, a palette shift of 6/255 and a one-word
    caption edit both move fewer pixels than the cross-platform font noise, so
    any tolerance wide enough to be portable is too wide to catch a real edit.

    So the staleness question is answered without pixels at all: this file
    records a fingerprint of every input that can change the rendering, and the
    test recomputes it.  That check is exact, has no tolerance, and gives the
    same answer on every machine.  The byte-for-byte comparison still runs, but
    only where the recorded environment matches -- see
    :mod:`tests.test_previews`.
    """
    environment = describe_environment()
    environment["fingerprint"] = fingerprint_environment(environment)
    document = {
        "generated_by": "python -m tools.generate_previews",
        "source_fingerprint": fingerprint_sources(),
        "environment": environment,
        "screenshots": {
            path.name: {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": list(pygame.image.load(str(path)).get_size()),
            }
            for path in sorted(written, key=lambda item: item.name)
        },
    }
    destination = output_dir / RENDER_MANIFEST_FILENAME
    destination.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return destination


def read_render_manifest(directory: Path = SCREENSHOTS_DIR) -> dict:
    """Load the sidecar written by :func:`write_render_manifest`."""
    path = directory / RENDER_MANIFEST_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output", type=Path, default=SCREENSHOTS_DIR, help="destination directory"
    )
    parser.add_argument(
        "--manifest", type=Path, default=MANIFEST_FILE, help="game manifest to render"
    )
    args = parser.parse_args(argv)

    written = generate(args.output, args.manifest)
    for path in written:
        print(f"wrote {path}")
    print(f"wrote {write_render_manifest(args.output, written)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
