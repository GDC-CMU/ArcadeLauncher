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
from typing import Callable, Sequence

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

    idle_delay_ms: int = 30_000
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
        #: Manifest indices attract may settle on this tick -- see
        #: :meth:`tick`. Kept as instance state so :meth:`_advance` can see
        #: it without threading it through every internal call.
        self._eligible: tuple[int, ...] = ()

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
            the gallery wherever attract had wandered off to. The idle clock
            always restarts from zero here, active or not, which is what
            lets attract trigger again after being dismissed: the very next
            full :attr:`~AttractConfig.idle_delay_ms` of genuine silence
            re-arms it exactly like the first time.
        """
        self._idle_ms = 0
        was_active = self._active
        self._active = False
        return was_active

    def tick(
        self,
        delta_ms: int,
        count: int,
        current_index: int,
        eligible_indices: Sequence[int],
    ) -> AttractSnapshot | None:
        """Advance by *delta_ms* and return this frame's attract state, if any.

        Args:
            count: How many games are currently selectable in total (used
                only for wrapping the *scrolling* index, which glides
                through the whole catalogue -- coming-soon cards included --
                on its way to a target).
            current_index: The gallery's real, current selection -- only used
                the moment attract *triggers*, as the starting point attract
                glides away from. Ignored on every later call.
            eligible_indices: Which manifest indices attract may actually
                *settle* on -- launchable, currently playable, and carrying a
                usable preview animation (see
                ``GallerySession._attract_eligible_indices``). A coming-soon
                card, or a launchable game with no preview yet, would sit
                static for the whole dwell period, which reads as broken
                rather than as a showcase, so those are never chosen as a
                target even though scrolling still glides past them. Checked
                fresh every call rather than cached at trigger time, so a
                catalogue that loses its last eligible game mid-demo (e.g. a
                sync failure) stops attract rather than settling on nothing.

        Returns:
            ``None`` while idle (not yet triggered), if *count* is zero, or
            if *eligible_indices* is empty -- attract never engages with
            nothing worth showing. Otherwise the state to display this frame.
        """
        self._eligible = tuple(eligible_indices)
        if count <= 0 or not self._eligible:
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
        self._start_leg()

    def _start_leg(self) -> None:
        self._phase = AttractPhase.SCROLLING
        self._phase_ms = 0
        self._target_index = self._rng.choice(self._eligible)

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
            if self._phase_ms >= self.config.settle_ms and len(self._eligible) > 1:
                # With only one eligible game there is nowhere else worth
                # settling on: switching view mode would be an instant,
                # scroll-less jump cutting straight back to the same card --
                # exactly the "jarring" cycling the client asked to avoid.
                # Simplest reading that still looks intentional: stay
                # settled here, in this mode, on this game, indefinitely.
                # The preview keeps looping on its own clock regardless.
                self._view_mode = self._next_view_mode()
                self._start_leg()

    def _next_view_mode(self) -> ViewMode:
        others = [mode for mode in ViewMode if mode is not self._view_mode]
        return self._rng.choice(others) if others else self._view_mode
