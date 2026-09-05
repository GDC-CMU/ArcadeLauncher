"""Attract mode: the idle-triggered demo state machine.

Deliberately free of Pygame: this module only ever decides *what* the
gallery should look like while nobody is at the cabinet -- a view mode, a
selected index, and whether it is still gliding between games or has settled
on one long enough to play its preview -- and leaves *how* that gets drawn
entirely to the renderer and, critically, to the exact same eased
scroll/glide machinery a visitor's own stick press drives (see
``GallerySession._glide``/``_glide_linear``). Advancing the index is
delegated to an injected ``navigate`` callable so this reuses precisely the
view's own :meth:`~launcher.ui.views.base.GalleryView.navigate`, never a
second, parallel animation system.

Randomness is fully injected (``rng``), so attract's exact sequence of view
modes and target games is reproducible in a test.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .input_state import Direction
from .viewmodes import ViewMode

__all__ = [
    "AttractPhase",
    "AttractConfig",
    "AttractSnapshot",
    "AttractController",
    "NavigateFn",
]

#: Advances *index* one step towards a leg's target, using the same
#: navigation a real stick press would -- see the module docstring.
NavigateFn = Callable[[ViewMode, int, int, Direction], int]


class AttractPhase(Enum):
    """Where one attract "leg" (one view mode, one target game) currently is."""

    #: Gliding towards this leg's target game, one step at a time.
    SCROLLING = "scrolling"
    #: Arrived; the target game's preview animation is playing in its card.
    SETTLED = "settled"


@dataclass(frozen=True, slots=True)
class AttractConfig:
    """Timing for the attract state machine.

    Attributes:
        idle_delay_ms: How long the gallery must see *zero* genuine input
            before attract triggers. The only one of these three exposed
            through :mod:`launcher.settings` -- see ``Settings.attract_idle_ms``.
        step_interval_ms: How long each scrolling step holds before the next.
        settle_ms: How long a settled game plays its preview before the next
            leg begins.
    """

    idle_delay_ms: int = 60_000
    step_interval_ms: int = 1_100
    settle_ms: int = 9_000

    def __post_init__(self) -> None:
        if self.idle_delay_ms < 0:
            raise ValueError("idle_delay_ms must not be negative")
        if self.step_interval_ms <= 0:
            raise ValueError("step_interval_ms must be positive")
        if self.settle_ms <= 0:
            raise ValueError("settle_ms must be positive")


@dataclass(frozen=True, slots=True)
class AttractSnapshot:
    """What the gallery should show this frame while attract is active.

    Attributes:
        view_mode: The composition attract has picked for this leg.
        index: The selection attract wants shown -- fed through exactly the
            same scroll/glide the real selection uses, never a separate one.
        phase: Whether this leg is still travelling or has settled.
        phase_elapsed_ms: Milliseconds since *phase* began. While
            :attr:`AttractPhase.SETTLED`, this is also the preview animation's
            own clock, restarting at zero every time a new game settles.
    """

    view_mode: ViewMode
    index: int
    phase: AttractPhase
    phase_elapsed_ms: int


class AttractController:
    """Drives the idle-triggered attract demo.

    Args:
        config: Timing constants; see :class:`AttractConfig`.
        rng: Source of randomness for view-mode and target-game choices.
            Defaults to a fresh, unseeded :class:`random.Random` -- pass a
            seeded one for a deterministic test.
        navigate: Callable that advances an index by one step in a given
            view mode and direction -- see :data:`NavigateFn`. This is always
            the real ``GalleryView.navigate``; attract never invents its own
            stepping rule.
    """

    def __init__(
        self,
        config: AttractConfig | None = None,
        *,
        rng: random.Random | None = None,
        navigate: NavigateFn,
    ) -> None:
        self.config = config or AttractConfig()
        self._rng = rng if rng is not None else random.Random()
        self._navigate = navigate
        self._active = False
        self._idle_ms = 0
        self._phase = AttractPhase.SCROLLING
        self._phase_ms = 0
        self._view_mode = ViewMode.GRID
        self._index = 0
        self._target_index = 0

    @property
    def active(self) -> bool:
        return self._active

    def reset(self) -> None:
        """Return to a freshly-opened, never-yet-idle state.

        Called when a gallery session opens (or reopens after a game
        returns) so a game that ran for a while cannot leave attract idle
        time "pre-charged" -- a visitor who just finished playing sees a
        full idle delay before attract can trigger again, exactly like a
        fresh visitor would.
        """
        self._active = False
        self._idle_ms = 0

    def notice_input(self) -> bool:
        """Reset the idle clock; deactivate attract if it was running.

        Returns:
            Whether attract *was* active -- the caller uses this to know it
            must restore the pre-attract selection/view rather than leaving
            the gallery wherever attract had wandered off to.
        """
        self._idle_ms = 0
        was_active = self._active
        self._active = False
        return was_active

    def tick(self, delta_ms: int, count: int, current_index: int) -> AttractSnapshot | None:
        """Advance by *delta_ms* and return this frame's attract state, if any.

        Args:
            count: How many games are currently selectable. Re-checked every
                call rather than cached at trigger time so a catalogue of
                exactly one game degrades gracefully -- attract simply always
                settles on the one game it has, rather than crashing trying
                to pick "a different" target.
            current_index: The gallery's real, current selection -- only used
                the moment attract *triggers*, as the starting point attract
                glides away from. Ignored on every later call.

        Returns:
            ``None`` while idle (not yet triggered) or if *count* is zero;
            otherwise the state to display this frame.
        """
        if count <= 0:
            self._active = False
            return None
        if not self._active:
            self._idle_ms += delta_ms
            if self._idle_ms < self.config.idle_delay_ms:
                return None
            self._begin(count, current_index)
        else:
            self._advance(delta_ms, count)
        return AttractSnapshot(self._view_mode, self._index, self._phase, self._phase_ms)

    # -- internals ----------------------------------------------------
    def _begin(self, count: int, current_index: int) -> None:
        self._active = True
        self._view_mode = self._rng.choice(tuple(ViewMode))
        self._index = current_index % count
        self._start_leg(count)

    def _start_leg(self, count: int) -> None:
        self._phase = AttractPhase.SCROLLING
        self._phase_ms = 0
        self._target_index = self._rng.randrange(count)

    def _advance(self, delta_ms: int, count: int) -> None:
        self._phase_ms += delta_ms
        if self._phase is AttractPhase.SCROLLING:
            while (
                self._phase_ms >= self.config.step_interval_ms
                and self._index != self._target_index
            ):
                self._phase_ms -= self.config.step_interval_ms
                self._index = (
                    self._navigate(self._view_mode, self._index, count, Direction.RIGHT)
                    % count
                )
            if self._index == self._target_index:
                self._phase = AttractPhase.SETTLED
                self._phase_ms = 0
        else:
            if self._phase_ms >= self.config.settle_ms:
                self._view_mode = self._next_view_mode()
                self._start_leg(count)

    def _next_view_mode(self) -> ViewMode:
        others = [mode for mode in ViewMode if mode is not self._view_mode]
        return self._rng.choice(others) if others else self._view_mode
