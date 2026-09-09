"""Pure launch argument construction; never prepare, download, or invoke a shell."""

from __future__ import annotations

import sys
from pathlib import Path

from .errors import LaunchError, NotLaunchableError
from .integrity import filesystem_path
from .manifest import GameEntry, Runtime, safe_relative_path


def build_child_command(
    entry: GameEntry, checkout: Path, *, executable: Path | None = None,
) -> list[str]:
    """Build a direct command for an already prepared game.

    Omitting ``executable`` is supported only for legacy Python entries without
    requirements. Native engines and isolated interpreters come from the
    preparation service's verified ``PreparedGame``, not the manifest or PATH.
    """
    if not entry.launchable:
        raise NotLaunchableError(f"game '{entry.id}' is coming-soon and must never be launched")
    absolute = entry.resolved_entrypoint(checkout)
    if not filesystem_path(absolute).is_file():
        raise LaunchError(f"game '{entry.id}': entrypoint '{entry.entrypoint}' does not exist in {checkout}")
    if executable is not None and not filesystem_path(executable).is_file():
        raise LaunchError(f"game '{entry.id}': prepared executable is missing: {executable}")
    if entry.runtime is Runtime.PYTHON:
        if entry.godot_version is not None or entry.startup_script is not None:
            raise LaunchError(f"game '{entry.id}': Python cannot use Godot launch metadata")
        if entry.python_requirements and executable is None:
            raise LaunchError(f"game '{entry.id}': isolated Python dependencies have not been prepared")
        assert entry.entrypoint is not None
        # A file literally named "-c" must not become a Python interpreter flag.
        script = entry.entrypoint
        if script.startswith("-"):
            script = "./" + script
        return [str(executable) if executable is not None else sys.executable, script]
    if entry.runtime is not Runtime.GODOT:
        raise LaunchError(f"game '{entry.id}': unsupported runtime")
    if executable is None or entry.godot_version is None:
        raise LaunchError(f"game '{entry.id}': pinned Godot runtime has not been prepared")
    if entry.python_requirements is not None:
        raise LaunchError(f"game '{entry.id}': Godot cannot use Python requirements")
    command = [str(executable)]
    if absolute.suffix == ".pck":
        command.extend(["--path", str(checkout.resolve()), "--main-pack", str(absolute)])
    elif absolute.name == "project.godot":
        command.extend(["--path", str(absolute.parent)])
    else:
        raise LaunchError(f"game '{entry.id}': expected .pck or project.godot")
    if entry.startup_script is not None:
        script_path = safe_relative_path(
            entry.startup_script, checkout, game_id=entry.id, field_name="startup_script",
        )
        if absolute.suffix != ".pck" or script_path.suffix != ".gd" or not filesystem_path(script_path).is_file():
            raise LaunchError(f"game '{entry.id}': pack startup_script is missing or invalid")
        command.extend(["--script", str(script_path)])
    command.extend(["--rendering-method", "gl_compatibility", "--fullscreen"])
    return command
