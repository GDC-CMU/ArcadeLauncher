"""Navigation input state: edge detection plus controlled key repeat.

Deliberately free of any Pygame import so the debounce rules can be tested
exactly, with an injected clock and no hardware.  :mod:`launcher.gallery` is
the only place that translates SDL events into calls on these objects.

The behaviour a visitor feels:

* Pushing the stick moves the selection **once**, immediately (edge detection).
* Holding it does nothing for ``initial_delay_ms`` -- so a nudge is a nudge.
* Keeping it held then repeats every ``repeat_ms``, which is slow enough that
  the gallery never races past the game you were looking at.
* Releasing resets everything, so the next push is instant again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

__all__ = [
    "Direction",
    "RepeatPolicy",
    "DirectionRepeater",
    "AxisAggregator",
    "NavigationController",
]


class Direction(Enum):
    """A cardinal navigation step."""

    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


@dataclass(frozen=True, slots=True)
class RepeatPolicy:
    """Timing for auto-repeat.

    Attributes:
        initial_delay_ms: Hold time before repeating starts.
        repeat_ms: Interval between repeats once repeating.
    """

    initial_delay_ms: int = 380
    repeat_ms: int = 140

    def __post_init__(self) -> None:
        if self.initial_delay_ms < 0:
            raise ValueError("initial_delay_ms must not be negative")
        if self.repeat_ms <= 0:
            raise ValueError("repeat_ms must be positive")


@dataclass(slots=True)
class _HoldState:
    pressed_at_ms: int
    last_fire_ms: int
    repeating: bool = False


class DirectionRepeater:
    """Turns a *held* direction set into discrete navigation steps.

    Each direction keeps its own timer, so a diagonal push on the arcade stick
    steps both axes rather than dropping one.
    """

    def __init__(self, policy: RepeatPolicy | None = None) -> None:
        self.policy = policy or RepeatPolicy()
        self._holds: dict[Direction, _HoldState] = {}

    def reset(self) -> None:
        """Forget all hold state (used when returning from a game)."""
        self._holds.clear()

    @property
    def held(self) -> frozenset[Direction]:
        return frozenset(self._holds)

    def update(self, held: Iterable[Direction], now_ms: int) -> tuple[Direction, ...]:
        """Advance to *now_ms* and return the steps to apply this frame.

        Args:
            held: Directions currently pushed (keyboard and/or stick, merged).
            now_ms: Milliseconds since launcher start; must be non-decreasing.

        Returns:
            Zero or more directions, in a stable order.
        """
        current = frozenset(held)

        for direction in list(self._holds):
            if direction not in current:
                del self._holds[direction]

        fired: list[Direction] = []
        for direction in Direction:
            if direction not in current:
                continue
            state = self._holds.get(direction)
            if state is None:
                # Rising edge: act immediately, then go quiet for the delay.
                self._holds[direction] = _HoldState(
                    pressed_at_ms=now_ms, last_fire_ms=now_ms
                )
                fired.append(direction)
                continue
            if not state.repeating:
                if now_ms - state.pressed_at_ms >= self.policy.initial_delay_ms:
                    state.repeating = True
                    state.last_fire_ms = now_ms
                    fired.append(direction)
                continue
            if now_ms - state.last_fire_ms >= self.policy.repeat_ms:
                state.last_fire_ms = now_ms
                fired.append(direction)
        return tuple(fired)


class AxisAggregator:
    """Merges the analogue axes of every connected joystick.

    The cabinet exposes a *digital* stick, so a deadzone of 0.5 -- matching the
    reference ``joystick.py`` -- is the right test for "pushed".  Sticks are
    keyed by SDL instance id, so hot-plugging one does not disturb the other.
    """

    def __init__(self, deadzone: float = 0.5) -> None:
        if not 0.0 < deadzone < 1.0:
            raise ValueError("deadzone must be between 0 and 1")
        self.deadzone = deadzone
        self._axes: dict[int, dict[int, float]] = {}

    @property
    def device_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._axes))

    def attach(self, instance_id: int) -> None:
        """Register a joystick (idempotent)."""
        self._axes.setdefault(instance_id, {})

    def detach(self, instance_id: int) -> None:
        """Forget a joystick and any direction it was holding (idempotent)."""
        self._axes.pop(instance_id, None)

    def set_axis(self, instance_id: int, axis: int, value: float) -> None:
        """Record an axis sample; unknown devices are attached on the fly."""
        self._axes.setdefault(instance_id, {})[axis] = float(value)

    def clear(self) -> None:
        """Drop every device (used when SDL is released before a launch)."""
        self._axes.clear()

    def held_directions(self) -> frozenset[Direction]:
        """Directions currently pushed on *any* connected stick."""
        from .controls import AXIS_HORIZONTAL, AXIS_VERTICAL

        directions: set[Direction] = set()
        for axes in self._axes.values():
            horizontal = axes.get(AXIS_HORIZONTAL, 0.0)
            vertical = axes.get(AXIS_VERTICAL, 0.0)
            if horizontal <= -self.deadzone:
                directions.add(Direction.LEFT)
            elif horizontal >= self.deadzone:
                directions.add(Direction.RIGHT)
            if vertical <= -self.deadzone:
                directions.add(Direction.UP)
            elif vertical >= self.deadzone:
                directions.add(Direction.DOWN)
        return frozenset(directions)


@dataclass(slots=True)
class NavigationController:
    """Keyboard + joystick navigation with one shared repeat policy."""

    repeater: DirectionRepeater = field(default_factory=DirectionRepeater)
    axes: AxisAggregator = field(default_factory=AxisAggregator)
    keyboard: set[Direction] = field(default_factory=set)

    @classmethod
    def from_policy(cls, policy: RepeatPolicy, deadzone: float) -> "NavigationController":
        return cls(repeater=DirectionRepeater(policy), axes=AxisAggregator(deadzone))

    def press_key(self, direction: Direction) -> None:
        self.keyboard.add(direction)

    def release_key(self, direction: Direction) -> None:
        self.keyboard.discard(direction)

    def reset(self) -> None:
        """Drop every held direction; called when the gallery regains focus."""
        self.keyboard.clear()
        self.axes.clear()
        self.repeater.reset()

    def poll(self, now_ms: int) -> tuple[Direction, ...]:
        """Return the navigation steps for this frame."""
        return self.repeater.update(self.keyboard | self.axes.held_directions(), now_ms)
