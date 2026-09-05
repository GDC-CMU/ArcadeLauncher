"""The gallery session: one Pygame run, from ``pygame.init`` to a clean release.

A session owns SDL for exactly as long as it runs.  It returns an explicit
:class:`~launcher.supervisor.UiOutcome` and, crucially, it **fully releases
SDL before returning** -- joysticks, display, mixer and font.  That is what
lets StreetFighter (whose ``pygame_compat`` calls ``pygame.init()`` at import
time) take the screen without fighting the launcher for it.

The session is deliberately not a class the supervisor has to understand: it is
a callable.  :class:`GallerySession` instances satisfy the supervisor's
``UiFactory`` protocol, so the supervisor only ever sees ``state -> outcome``.
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Callable

from .controls import BUTTON_EXIT, Command, command_for_button
from .input_state import Direction, NavigationController, RepeatPolicy
from .manifest import Manifest
from .settings import Settings
from .status import GameState, GameStatus
from .supervisor import SessionState, UiAction, UiOutcome
from .sync import SyncService
from .ui import SCREEN_SIZE
from .ui.pygame_runtime import pygame
from .ui.scene import Renderer
from .ui.viewmodel import GalleryFrame, Toast
from .ui.views.grid import target_scroll as grid_target_scroll
from .viewmodes import ViewMode

__all__ = ["KEY_COMMANDS", "KEY_DIRECTIONS", "GallerySession"]

_log = logging.getLogger(__name__)

#: Keyboard equivalents for every cabinet control (acceptance criterion E2).
KEY_COMMANDS: dict[int, Command] = {
    pygame.K_RETURN: Command.LAUNCH,
    pygame.K_KP_ENTER: Command.LAUNCH,
    pygame.K_SPACE: Command.LAUNCH,
    pygame.K_ESCAPE: Command.EXIT,
    pygame.K_TAB: Command.CYCLE_VIEW,
    pygame.K_1: Command.VIEW_GRID,
    pygame.K_2: Command.VIEW_CAROUSEL,
    pygame.K_3: Command.VIEW_COVER_FLOW,
    pygame.K_KP1: Command.VIEW_GRID,
    pygame.K_KP2: Command.VIEW_CAROUSEL,
    pygame.K_KP3: Command.VIEW_COVER_FLOW,
}

#: Arrows *and* WASD, held rather than tapped, so repeat behaves identically.
KEY_DIRECTIONS: dict[int, Direction] = {
    pygame.K_LEFT: Direction.LEFT,
    pygame.K_a: Direction.LEFT,
    pygame.K_RIGHT: Direction.RIGHT,
    pygame.K_d: Direction.RIGHT,
    pygame.K_UP: Direction.UP,
    pygame.K_w: Direction.UP,
    pygame.K_DOWN: Direction.DOWN,
    pygame.K_s: Direction.DOWN,
}

_VIEW_COMMANDS: dict[Command, ViewMode] = {
    Command.VIEW_GRID: ViewMode.GRID,
    Command.VIEW_CAROUSEL: ViewMode.CAROUSEL,
    Command.VIEW_COVER_FLOW: ViewMode.COVER_FLOW,
}

_TOAST_MS = 1600

#: How long after a session opens Command.EXIT is refused outright.
#:
#: A game quits itself with Esc or P1 (button 5); the instant it exits, this
#: process reopens the display and starts pumping events again. If the visitor
#: is still physically holding that same key or button -- entirely plausible,
#: since it is the same input that just closed the game -- the freshly opened
#: gallery must not read that as "leave the gallery too" and bounce the
#: visitor straight past it to the arcade menu. A window this short is
#: imperceptible as a delay but comfortably outlasts a human release.
_EXIT_SETTLE_MS = 300


def _never() -> bool:
    """Default shutdown predicate: a session that is never asked to stop."""
    return False


class GallerySession:
    """Runs one gallery session and reports what the visitor asked for.

    Args:
        manifest: The validated game list.
        settings: Resolved configuration (view mode, frame rate, timings).
        states: Live availability per game id. Mutated in place by
            :meth:`_absorb_sync` as background syncs finish.
        sync: Optional background sync service. ``None`` disables updates,
            which is what the offline tests use.
        should_stop: Polled once per frame; when it returns ``True`` the
            session ends as though the visitor had exited. This is how a
            SIGTERM reaches the loop. Without it the supervisor's shutdown
            flag is only read *between* sessions, so an idle cabinet -- which
            never generates a QUIT of its own -- would run forever and have to
            be reset at the wall. Defaults to "never stop".
    """

    def __init__(
        self,
        manifest: Manifest,
        settings: Settings,
        states: dict[str, GameState],
        sync: SyncService | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.manifest = manifest
        self.settings = settings
        self.states = states
        self.sync = sync
        self.renderer = Renderer()
        self.navigation = NavigationController.from_policy(
            RepeatPolicy(
                initial_delay_ms=settings.nav_initial_delay_ms,
                repeat_ms=settings.nav_repeat_ms,
            ),
            deadzone=settings.axis_deadzone,
        )
        self._joysticks: dict[int, pygame.joystick.Joystick] = {}
        self._should_stop = should_stop if should_stop is not None else _never
        #: Keys/buttons already down when this session opened, mapped to
        #: Command.EXIT; suppressed until released so a key still physically
        #: held from before the transition cannot fire an immediate exit.
        self._held_exit_keys: set[int] = set()
        self._held_exit_buttons: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------
    # Entry point (the supervisor's UiFactory)
    # ------------------------------------------------------------------
    def __call__(self, state: SessionState) -> UiOutcome:
        """Run until the visitor launches a game or exits.

        SDL is initialised here and released in the ``finally`` block, so this
        method is safe to call repeatedly -- once per trip back from a game.
        """
        self._open_display()
        try:
            return self._loop(state)
        finally:
            self._release_sdl()

    # ------------------------------------------------------------------
    # SDL lifecycle
    # ------------------------------------------------------------------
    def _open_display(self) -> None:
        pygame.display.init()
        pygame.font.init()
        pygame.joystick.init()

        flags = pygame.SCALED
        if self.settings.fullscreen:
            flags |= pygame.FULLSCREEN
        with warnings.catch_warnings():
            # SCALED asks SDL for a hardware-accelerated scaler so the cabinet
            # can letterbox 800x600 onto whatever panel is fitted. Where none
            # exists (a headless CI box, a bare framebuffer) SDL falls back to
            # software and warns; that fallback is fine and expected here.
            warnings.filterwarnings("ignore", message=".*fast renderer.*")
            pygame.display.set_mode(SCREEN_SIZE, flags)
        pygame.display.set_caption("GDC Arcade")
        pygame.mouse.set_visible(False)  # criterion G5: nothing is mouse-driven

        self.navigation.reset()
        self._joysticks.clear()
        self._held_exit_keys.clear()
        self._held_exit_buttons.clear()
        for index in range(pygame.joystick.get_count()):
            self._attach_joystick(index)

        # A previous session (or the child game that just exited) can leave
        # events sitting in SDL's queue; none of them describe *this* session
        # and must never be read as an immediate command -- see item 1.
        pygame.event.clear()
        self._prime_held_exit_keys()

    def _release_sdl(self) -> None:
        """Give the display and every joystick back to the operating system.

        Criterion F2.  Each step is guarded independently: a failure to close
        one joystick must not prevent the display from being released, or the
        next game would start into a dead screen.
        """
        for instance_id, stick in list(self._joysticks.items()):
            try:
                stick.quit()
            except pygame.error as exc:
                _log.warning("could not close joystick %s: %s", instance_id, exc)
        self._joysticks.clear()
        self.navigation.reset()

        for name, release in (
            ("joystick", pygame.joystick.quit),
            ("display", pygame.display.quit),
            ("font", pygame.font.quit),
        ):
            try:
                release()
            except pygame.error as exc:
                _log.warning("could not release SDL %s subsystem: %s", name, exc)
        pygame.quit()
        _log.debug("SDL released")

    def _attach_joystick(self, index: int) -> None:
        """Open and register the device at *index*, at most once.

        SDL announces a device that is already plugged in twice at start-up:
        once through the ``get_count()`` enumeration this class does on open,
        and again as a ``JOYDEVICEADDED`` event in the first frames of the
        loop. The device index has to be opened before its stable instance id
        can be read, so the duplicate is detected here and its handle closed
        rather than left to leak. Keying on the instance id -- not the device
        index, which SDL reuses -- is what makes this idempotent however the
        device is discovered.
        """
        try:
            stick = pygame.joystick.Joystick(index)
            stick.init()
        except pygame.error as exc:
            _log.warning("joystick %s could not be opened: %s", index, exc)
            return
        instance_id = stick.get_instance_id()

        known = self._joysticks.get(instance_id)
        if known is not None:
            if known is not stick:
                try:
                    stick.quit()
                except pygame.error as exc:
                    _log.debug("duplicate joystick handle %s: %s", instance_id, exc)
            _log.debug("joystick instance %s already attached", instance_id)
            return

        self._joysticks[instance_id] = stick
        self.navigation.axes.attach(instance_id)
        if stick.get_numbuttons() > BUTTON_EXIT and stick.get_button(BUTTON_EXIT):
            # The exit button was already down the instant this stick was
            # opened -- almost certainly the same press that just closed the
            # game. Suppress it until it is released; see item 1.
            self._held_exit_buttons.add((instance_id, BUTTON_EXIT))
        _log.info("joystick attached: %s (instance %s)", stick.get_name(), instance_id)

    def _prime_held_exit_keys(self) -> None:
        """Note any exit-mapped key already down the instant the session opens.

        ``pygame.key.get_pressed()`` reflects real key state independently of
        the event queue, so this catches a physically-held key even though
        :meth:`_open_display` has just cleared any stale queued events.
        """
        pressed = pygame.key.get_pressed()
        self._held_exit_keys = {
            key
            for key, command in KEY_COMMANDS.items()
            if command is Command.EXIT and key < len(pressed) and pressed[key]
        }

    def _detach_joystick(self, instance_id: int) -> None:
        stick = self._joysticks.pop(instance_id, None)
        if stick is not None:
            try:
                stick.quit()
            except pygame.error as exc:
                _log.debug("joystick %s already gone: %s", instance_id, exc)
        self.navigation.axes.detach(instance_id)
        self._held_exit_buttons.discard((instance_id, BUTTON_EXIT))
        _log.info("joystick detached: instance %s", instance_id)

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------
    def _tick(self, clock: pygame.time.Clock) -> int:
        """Advance one frame and return the elapsed milliseconds.

        A seam, like :meth:`_pump`: the cabinet paces itself against the real
        clock, but a test that exercises auto-repeat needs the frame delta to
        be *its* choice rather than however long the machine happened to take.
        Without this the repeat timings drift under load and the loop's
        behaviour becomes a function of CPU speed.
        """
        return clock.tick(self.settings.frame_rate)

    def _loop(self, state: SessionState) -> UiOutcome:
        screen = pygame.display.get_surface()
        clock = pygame.time.Clock()

        index = min(max(state.selected_index, 0), len(self.manifest) - 1)
        mode = state.view_mode
        notice = state.notice
        toast: Toast | None = None
        scroll = float(index)
        grid_scroll = grid_target_scroll(index, len(self.manifest))
        focus_ms = 0
        elapsed_ms = 0

        if self.sync is not None and self.settings.sync_on_start:
            self.sync.request_all(self.manifest.launchable)

        while True:
            # Checked first, and every frame: a termination request must be
            # observed while the session is running, not merely between
            # sessions. On an idle cabinet there is no other way out.
            if self._should_stop():
                _log.info("shutdown requested; leaving the gallery")
                return self._outcome(UiAction.QUIT, mode, index)

            delta_ms = self._tick(clock)
            elapsed_ms += delta_ms
            focus_ms += delta_ms
            self._absorb_sync()

            for event in self._pump():
                command = self._command_for(event)
                if event.type == pygame.QUIT:
                    return self._outcome(UiAction.QUIT, mode, index)
                if event.type == pygame.JOYDEVICEADDED:
                    self._attach_joystick(event.device_index)
                elif event.type == pygame.JOYDEVICEREMOVED:
                    self._detach_joystick(event.instance_id)
                elif event.type == pygame.JOYAXISMOTION:
                    self.navigation.axes.set_axis(
                        event.instance_id, event.axis, event.value
                    )
                elif event.type == pygame.KEYDOWN and event.key in KEY_DIRECTIONS:
                    self.navigation.press_key(KEY_DIRECTIONS[event.key])
                elif event.type == pygame.KEYUP:
                    self._held_exit_keys.discard(event.key)
                    if event.key in KEY_DIRECTIONS:
                        self.navigation.release_key(KEY_DIRECTIONS[event.key])
                elif event.type == pygame.JOYBUTTONUP:
                    self._held_exit_buttons.discard((event.instance_id, event.button))

                if command is None:
                    continue
                if command is Command.EXIT:
                    if self._exit_is_suppressed(event, elapsed_ms):
                        _log.debug("ignoring exit: stale or settling input")
                        continue
                    _log.info("visitor asked to leave the gallery")
                    return self._outcome(UiAction.QUIT, mode, index)
                if command is Command.CYCLE_VIEW:
                    mode = mode.next()  # selection is untouched: criterion D7
                    notice = None
                elif command in _VIEW_COMMANDS:
                    mode = _VIEW_COMMANDS[command]
                    notice = None
                elif command is Command.LAUNCH:
                    entry = self.manifest[index]
                    status = self._status(entry.id)
                    if entry.launchable and status.is_playable:
                        return self._outcome(UiAction.LAUNCH, mode, index, entry.id)
                    toast = self._refusal(index, status, elapsed_ms)
                    notice = None

            steps = self.navigation.poll(elapsed_ms)
            if steps:
                view = self.renderer.view(mode)
                for direction in steps:
                    index = view.navigate(index, len(self.manifest), direction)
                focus_ms = 0
                notice = None

            scroll = self._glide(scroll, index, delta_ms, len(self.manifest))
            grid_scroll = self._glide_linear(
                grid_scroll, grid_target_scroll(index, len(self.manifest)), delta_ms
            )
            if toast is not None and toast.is_expired(elapsed_ms):
                toast = None

            frame = GalleryFrame.build(
                self.manifest,
                self.states,
                selected_index=index,
                view_mode=mode,
                time_ms=elapsed_ms,
                scroll=scroll,
                grid_scroll=grid_scroll,
                focus_ms=focus_ms,
                notice=notice,
                toast=toast,
            )
            self.renderer.draw(screen, frame)
            pygame.display.flip()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _pump(self) -> list[pygame.event.Event]:
        """Return this frame's input events.

        The only place the loop touches the SDL queue, and therefore the seam
        the tests replace to replay a scripted sequence deterministically
        instead of racing a real event queue.
        """
        return pygame.event.get()

    @staticmethod
    def _command_for(event: pygame.event.Event) -> Command | None:
        """Resolve a raw SDL event to a command, or ``None``."""
        if event.type == pygame.KEYDOWN:
            return KEY_COMMANDS.get(event.key)
        if event.type == pygame.JOYBUTTONDOWN:
            return command_for_button(event.button)
        return None

    def _exit_is_suppressed(self, event: pygame.event.Event, elapsed_ms: int) -> bool:
        """Whether an EXIT command must be ignored -- see item 1.

        Two independent guards, either of which is enough to suppress:

        * The settle window: for a short moment after the session opens, no
          EXIT fires at all, so a KEYDOWN that was already queued (or that SDL
          synthesises when the window regains input focus) cannot bounce the
          visitor straight past a gallery they never got to see.
        * The held-since-open check: a key or button recorded as already down
          in :meth:`_open_display` stays suppressed until its release, which
          catches a press that spans the transition even if it arrives after
          the settle window (a long hold, or a slow-to-exit child process).
        """
        if elapsed_ms < _EXIT_SETTLE_MS:
            return True
        if event.type == pygame.KEYDOWN:
            return event.key in self._held_exit_keys
        if event.type == pygame.JOYBUTTONDOWN:
            return (event.instance_id, event.button) in self._held_exit_buttons
        return False

    @staticmethod
    def _glide(scroll: float, index: int, delta_ms: int, count: int) -> float:
        """Ease the smoothed scroll position towards the real selection.

        The horizontal modes read this instead of the integer index so movement
        looks like motion rather than teleportation. The step always takes the
        *shortest* path around the wrap -- crossing from the last card to the
        first glides forward by one slot rather than sweeping backwards across
        every card in between -- and the result is kept inside ``0..count`` so
        a long session cannot drift the float arbitrarily far from the index
        it is tracking.
        """
        if count <= 0:
            return float(index)
        raw_distance = index - scroll
        distance = ((raw_distance + count / 2) % count) - count / 2
        if abs(distance) < 0.01:
            return float(index % count)
        return (scroll + distance * min(1.0, delta_ms / 90.0)) % count

    @staticmethod
    def _glide_linear(current: float, target: float, delta_ms: int) -> float:
        """Ease *current* towards *target* using the same frame-delta-driven
        approach as :meth:`_glide`, without the wrap-around distance
        calculation -- the Grid view's vertical row scroll is a straight
        line, not a cycle, so there is no "shortest way around" to take.
        """
        distance = target - current
        if abs(distance) < 0.01:
            return target
        return current + distance * min(1.0, delta_ms / 90.0)

    def _status(self, game_id: str) -> GameStatus:
        state = self.states.get(game_id)
        return state.status if state is not None else GameStatus.PENDING

    def _refusal(self, index: int, status: GameStatus, now_ms: int) -> Toast:
        """Explain why a card did not launch. Criterion E6 -- never launches."""
        entry = self.manifest[index]
        if status is GameStatus.COMING_SOON:
            headline, detail = "COMING SOON", f"{entry.title} is not on the cabinet yet"
        elif status.is_busy:
            headline, detail = "STILL SYNCING", f"{entry.title} is still downloading"
        else:
            headline, detail = "NOT AVAILABLE", f"{entry.title} could not be downloaded"
        _log.info("refused to launch %s (%s)", entry.id, status.value)
        return Toast(headline, detail, started_ms=now_ms, duration_ms=_TOAST_MS)

    def _absorb_sync(self) -> None:
        """Fold any finished background sync results into the live state map."""
        if self.sync is None:
            return
        for state in self.sync.drain():
            self.states[state.game_id] = state

    def _outcome(
        self, action: UiAction, mode: ViewMode, index: int, game_id: str | None = None
    ) -> UiOutcome:
        return UiOutcome(
            action=action, view_mode=mode, selected_index=index, game_id=game_id
        )
