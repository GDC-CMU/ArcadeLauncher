"""Control bindings for the arcade cabinet and for keyboard development.

The arcade numbers below come from the CMU 15-112 arcade-box startercode
``Key-Map.md``.  There is no button 6 or 7 on the cabinet, and there is no
separate P2 button, so nothing here invents one.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "Command",
    "AXIS_HORIZONTAL",
    "AXIS_VERTICAL",
    "BUTTON_LAUNCH",
    "BUTTON_EXIT",
    "BUTTON_CYCLE_VIEW",
    "ARCADE_BUTTON_COMMANDS",
    "command_for_button",
]


class Command(Enum):
    """A resolved user intent, independent of where the input came from."""

    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    LAUNCH = "launch"
    CYCLE_VIEW = "cycle-view"
    EXIT = "exit"
    VIEW_GRID = "view-grid"
    VIEW_CAROUSEL = "view-carousel"
    VIEW_COVER_FLOW = "view-cover-flow"


#: Arcade stick axes. The stick is digital: values sit at -1, 0 or +1.
AXIS_HORIZONTAL = 0  # -1 = left, +1 = right
AXIS_VERTICAL = 1  # -1 = up,   +1 = down

#: Cabinet buttons used by the launcher.
BUTTON_LAUNCH = 1  # "A"
BUTTON_EXIT = 5  # "P1" -- the documented quit button
BUTTON_CYCLE_VIEW = 8  # "Select"

ARCADE_BUTTON_COMMANDS: dict[int, Command] = {
    BUTTON_LAUNCH: Command.LAUNCH,
    BUTTON_EXIT: Command.EXIT,
    BUTTON_CYCLE_VIEW: Command.CYCLE_VIEW,
}


def command_for_button(button: int | str) -> Command | None:
    """Map an arcade button id to a :class:`Command`.

    The startercode's own ``joystick.py`` hands out button identifiers as
    *strings* while Pygame's events use *ints*, so both are accepted.

    Returns:
        The bound command, or ``None`` for an unbound button.
    """
    try:
        number = int(button)
    except (TypeError, ValueError):
        return None
    return ARCADE_BUTTON_COMMANDS.get(number)
