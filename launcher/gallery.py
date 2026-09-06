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
import random
import warnings
from collections.abc import Callable
from pathlib import Path

from .attract import AttractConfig, AttractController, AttractPhase
from .controls import BUTTON_EXIT, Command, command_for_button
from .input_state import Direction, NavigationController, RepeatPolicy
from .manifest import GameEntry, Manifest
from .settings import Settings
from .status import GameState, GameStatus
from .supervisor import SessionState, UiAction, UiOutcome
from .sync import SyncService
from .ui import SCREEN_SIZE
from .ui.components import RenderContext
from .ui.preview import PreviewLibrary
from .ui.pygame_runtime import pygame
from .ui.scene import Renderer
from .ui.viewmodel import GalleryFrame, PreviewPlayback, Toast
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

#: How long a session waits, after it opens, before trusting a
#: ``pygame.key.get_pressed()``/``Joystick.get_button()`` reading of "not
#: held" enough to arm an exit source on it -- see the docstring on
#: :meth:`GallerySession._sync_exit_arming` for the failure this guards
#: against. A prior value of 250ms was measured to still be too short: a
#: real window on the cabinet's environment was measured taking well over a
#: second to report input focus at all (``pygame.key.get_focused()`` never
#: turned true within 4s in that measurement), so 250ms after open, SDL's
#: keyboard state was still all-zero from lack of focus, not from release,
#: and the fallback armed a key that was still physically held. A full
#: second is long enough to comfortably outlast both a human's hold through
#: a game exit and a slow window-focus transition, while still costing a
#: visitor nothing perceptible: an EXIT press in the very first moment of a
#: freshly opened gallery is essentially always stale input carried over
#: from the game that just closed, never genuine intent -- nobody reacts to
#: a screen they have not registered seeing yet in under a second, so
#: refusing EXIT for that long risks nothing real.
_EXIT_ARM_FALLBACK_GRACE_MS = 1000


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
        sync: Optional startup sync service to drain. Its initial batch is
            queued by the entrypoint, not by each reopened gallery.
        should_stop: Polled once per frame; when it returns ``True`` the
            session ends as though the visitor had exited. This is how a
            SIGTERM reaches the loop. Without it the supervisor's shutdown
            flag is only read *between* sessions, so an idle cabinet -- which
            never generates a QUIT of its own -- would run forever and have to
            be reset at the wall. Defaults to "never stop".
        cache_root: Managed cache root a game's checkout is resolved inside,
            for attract mode's preview animations (see
            :mod:`launcher.ui.preview`). Defaults to the launcher's own
            default cache root; a session started with ``--cache`` should
            pass that same override through here so attract mode looks in
            the same place the supervisor actually launches games from.
        attract_rng: Source of randomness for attract mode's view-mode and
            target-game choices. Defaults to a fresh, unseeded
            :class:`random.Random`; a test passes a seeded one for a
            deterministic sequence.
    """

    def __init__(
        self,
        manifest: Manifest,
        settings: Settings,
        states: dict[str, GameState],
        sync: SyncService | None = None,
        should_stop: Callable[[], bool] | None = None,
        cache_root: Path | None = None,
        attract_rng: random.Random | None = None,
    ) -> None:
        self.manifest = manifest
        self.settings = settings
        self.states = states
        self.sync = sync
        #: Where a game's checkout lives, for attract mode's preview
        #: animations -- kept here (not baked into a renderer built once)
        #: because the renderer itself must be rebuilt every session; see
        #: :meth:`_open_display` and the attribute docstring on
        #: :attr:`renderer`.
        self._cache_root = cache_root
        #: Built fresh in :meth:`_open_display` and dropped in
        #: :meth:`_release_sdl` -- ``None`` only outside the lifetime of one
        #: SDL session. This must never survive across a teardown: every
        #: Surface and Font it caches (backgrounds, card art, the logo,
        #: decoded preview frames, and every ``pygame.font.Font`` a
        #: ``FontBook``/``PixelFont`` lazily creates) is freed at the C level
        #: the instant ``pygame.font.quit()``/``pygame.quit()`` run in
        #: ``_release_sdl``. A renderer built once in ``__init__`` and reused
        #: across the many sessions one ``GallerySession`` instance lives
        #: through would keep drawing with those same Python objects after
        #: the memory behind them was freed -- readable as "Couldn't find
        #: glyph" on the very next font lookup, then a native access
        #: violation on the next Surface blit, and it would take out the
        #: *error-notice* rendering path along with everything else, since
        #: that path uses the very same cached fonts. Rebuilding per session
        #: is what makes a stale reference structurally impossible rather
        #: than merely avoided.
        self.renderer: Renderer | None = None
        self.navigation = NavigationController.from_policy(
            RepeatPolicy(
                initial_delay_ms=settings.nav_initial_delay_ms,
                repeat_ms=settings.nav_repeat_ms,
            ),
            deadzone=settings.axis_deadzone,
        )
        self._attract = AttractController(
            AttractConfig(idle_delay_ms=settings.attract_idle_ms),
            rng=attract_rng,
            navigate=self._attract_navigate,
        )
        self._joysticks: dict[int, pygame.joystick.Joystick] = {}
        self._should_stop = should_stop if should_stop is not None else _never
        #: Exit-mapped keys/buttons *not yet* confirmed safe to fire -- see
        #: item 1 and :meth:`_sync_exit_arming`/:meth:`_exit_is_suppressed`.
        #: Every session opens with every exit-mapped key disarmed (there is
        #: always exactly one: Escape) and every currently-attached
        #: joystick's exit button disarmed too, regardless of whether either
        #: is actually held right now -- that determination is never trusted
        #: to a single sample taken this early (see the docstring on
        #: :meth:`_sync_exit_arming` for why). A source arms -- is removed
        #: from these sets -- the moment we positively observe it is *not*
        #: held: either a KEYUP/JOYBUTTONUP for it, or a later frame's
        #: recheck. Until then an EXIT command from it is ignored no matter
        #: how long it is held; once armed, EXIT is immediate.
        self._disarmed_exit_keys: set[int] = set()
        self._disarmed_exit_buttons: set[tuple[int, int]] = set()
        #: Exit-mapped keys/buttons directly observed as pressed via a real
        #: KEYDOWN/JOYBUTTONDOWN event since their last release -- belt and
        #: braces for :meth:`_sync_exit_arming`'s fallback: a source in here
        #: has actual event evidence of being down right now, not merely a
        #: ``get_pressed()``/``get_button()`` sample, so the fallback must
        #: never arm it no matter what that sample says or how much grace
        #: has elapsed. Only a KEYUP/JOYBUTTONUP removes an entry. A source
        #: that never appears here at all is the "never held" case the
        #: fallback exists for -- see the docstring there.
        self._observed_down_exit_keys: set[int] = set()
        self._observed_down_exit_buttons: set[tuple[int, int]] = set()

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

        # Built fresh every time SDL comes up -- see the docstring on
        # :attr:`renderer` in __init__ for why this may never be the same
        # object (or hold a single cached Surface/Font) a previous session
        # used.
        self.renderer = Renderer(RenderContext(previews=PreviewLibrary(self._cache_root)))
        self._on_renderer_ready()

        self.navigation.reset()
        self._attract.reset()
        self._joysticks.clear()
        # Disarmed by default -- see the attribute docstring in __init__.
        self._disarmed_exit_keys = {
            key for key, command in KEY_COMMANDS.items() if command is Command.EXIT
        }
        self._disarmed_exit_buttons = set()
        self._observed_down_exit_keys = set()
        self._observed_down_exit_buttons = set()
        for index in range(self._joystick_count()):
            self._attach_joystick(index)

        # A previous session (or the child game that just exited) can leave
        # events sitting in SDL's queue; none of them describe *this* session
        # and must never be read as an immediate command -- see item 1. Note
        # that no arming sync happens here: at elapsed_ms == 0 the fallback
        # grace (_EXIT_ARM_FALLBACK_GRACE_MS) has not been met, so it would
        # be a no-op anyway -- see :meth:`_sync_exit_arming`. Arming starts
        # from the loop's first frame.
        pygame.event.clear()

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
        # Every Surface and Font the renderer cached is now a dangling
        # pointer at the C level -- pygame.font.quit()/pygame.quit() just
        # freed the memory behind them. Dropping the whole renderer (rather
        # than trying to selectively clear its caches) is what guarantees
        # nothing here can be drawn with again before _open_display builds a
        # fresh one; see the attribute docstring in __init__.
        self.renderer = None
        _log.debug("SDL released")

    def _on_renderer_ready(self) -> None:
        """Called once :attr:`renderer` is freshly (re)built. A no-op here;
        a seam for tests that need to reach into a session's ``RenderContext``
        -- to register a fixture preview, or to wrap ``draw`` for recording --
        at the one moment it is guaranteed to exist and be current. See
        ``ScriptedSession`` in ``tests.test_gallery``.
        """
        return None

    def _joystick_count(self) -> int:
        """How many joystick devices to enumerate at open.

        A seam: tests stand in a fake joystick without a real SDL device, so
        this (and :meth:`_open_joystick`) is what they override instead of
        touching ``pygame.joystick`` directly.
        """
        return pygame.joystick.get_count()

    def _open_joystick(self, index: int) -> pygame.joystick.Joystick:
        """Open and initialise the device at *index*. See :meth:`_joystick_count`."""
        stick = pygame.joystick.Joystick(index)
        stick.init()
        return stick

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
            stick = self._open_joystick(index)
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
        # Disarmed by default, same as every exit-mapped key -- see item 1
        # and the attribute docstring in __init__. _sync_exit_arming (called
        # every frame in the loop, once its fallback grace period has
        # passed) arms it once the button turns out not to be held.
        self._disarmed_exit_buttons.add((instance_id, BUTTON_EXIT))
        _log.info("joystick attached: %s (instance %s)", stick.get_name(), instance_id)

    def _is_key_held(self, key: int) -> bool:
        """Ground truth: is *key* physically down right now?

        Used only by :meth:`_sync_exit_arming` as the fallback arming path --
        the primary one is the KEYUP event, handled directly in the loop.
        """
        pressed = pygame.key.get_pressed()
        return key < len(pressed) and bool(pressed[key])

    def _is_button_held(self, instance_id: int, button: int) -> bool:
        """Ground truth: is *button* on joystick *instance_id* down right now?

        Used only by :meth:`_sync_exit_arming`, same role as
        :meth:`_is_key_held`. A joystick that has since been detached counts
        as not held -- there is nothing left to release.
        """
        stick = self._joysticks.get(instance_id)
        if stick is None:
            return False
        return button < stick.get_numbuttons() and bool(stick.get_button(button))

    def _sync_exit_arming(self, elapsed_ms: int) -> None:
        """Arm every exit-mapped key/button not yet confirmed released.

        This is the fallback half of arm-on-release (item 1): the KEYUP/
        JOYBUTTONUP handling in the loop is the fast, primary path, and arms
        the instant SDL reports a release -- that stays exactly as-is and is
        trusted immediately, at any time, because a queued release event is
        real evidence.

        This fallback exists for the case that event never arrives -- most
        importantly, arming a source that was never held in the first place
        (the overwhelmingly common case, which has no release to wait for at
        all) but also a window that loses input focus while a key is up, so
        SDL never emits the KEYUP. Reaching for ``pygame.key.get_pressed()``/
        ``Joystick.get_button()`` here looks like it settles the question
        directly, but a regression proved it does not: immediately after
        ``pygame.display.set_mode()`` the freshly created window has not
        necessarily *gained input focus* yet, and until it does, SDL's
        keyboard state array reads as all-zero -- not because nothing is
        held, but because the window has not started receiving input at
        all. Pumping events first does not fix this; there is simply
        nothing to pump yet. A still-held Esc/P1 was therefore read as
        "released", armed on the spot, and fired for real the moment focus
        actually arrived and delivered the key's genuine, still-held state.

        So this only ever answers "not held" once :data:`_EXIT_ARM_FALLBACK_GRACE_MS`
        has passed since the session opened -- comfortably past the point a
        freshly created window has gained focus and its input state means
        what it says. Before that, this is a no-op and only the KEYUP/
        JOYBUTTONUP fast path above can arm anything, exactly as if nothing
        were held yet -- which is the safe assumption until proven otherwise.

        Belt and braces on top of the grace period: a source with a real
        KEYDOWN/JOYBUTTONDOWN event already recorded against it in
        ``_observed_down_exit_keys``/``_observed_down_exit_buttons`` is
        skipped outright, whatever ``get_pressed()``/``get_button()`` say.
        That set is direct event evidence, not a poll -- strictly stronger
        proof than any sample this method could take -- so once a source
        has shown itself down for real, only an actual KEYUP/JOYBUTTONUP
        may arm it, never a timing heuristic.
        """
        if not self._disarmed_exit_keys and not self._disarmed_exit_buttons:
            return
        if elapsed_ms < _EXIT_ARM_FALLBACK_GRACE_MS:
            return
        pygame.event.pump()
        for key in list(self._disarmed_exit_keys):
            if key in self._observed_down_exit_keys:
                continue
            if not self._is_key_held(key):
                self._disarmed_exit_keys.discard(key)
        for pair in list(self._disarmed_exit_buttons):
            if pair in self._observed_down_exit_buttons:
                continue
            if not self._is_button_held(*pair):
                self._disarmed_exit_buttons.discard(pair)

    def _detach_joystick(self, instance_id: int) -> None:
        stick = self._joysticks.pop(instance_id, None)
        if stick is not None:
            try:
                stick.quit()
            except pygame.error as exc:
                _log.debug("joystick %s already gone: %s", instance_id, exc)
        self.navigation.axes.detach(instance_id)
        self._disarmed_exit_buttons.discard((instance_id, BUTTON_EXIT))
        self._observed_down_exit_buttons.discard((instance_id, BUTTON_EXIT))
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
        #: The selection and view mode a visitor actually left the gallery
        #: on, captured the instant attract mode triggers and restored the
        #: instant any input cancels it -- ``None`` whenever attract is not
        #: (and was not, this frame) running. See the attract handling below.
        attract_saved: tuple[int, ViewMode] | None = None

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
            self._sync_exit_arming(elapsed_ms)

            # Attract mode: advance the idle timer, or -- once triggered --
            # the demo's own state machine. It only ever proposes a view mode
            # and a target index (``snapshot``), fed through exactly the
            # scroll/glide calls below a visitor's own stick press would
            # drive -- see the module docstring on :mod:`launcher.attract`.
            snapshot = self._attract.tick(
                delta_ms, len(self.manifest), index, self._attract_eligible_indices()
            )
            preview: PreviewPlayback | None = None
            if snapshot is None:
                if attract_saved is not None:
                    # Attract ended on its own rather than via input -- only
                    # reachable if the catalogue became empty mid-session.
                    # Restore the visitor's own selection rather than
                    # stranding it on whatever attract last showed.
                    index, mode = attract_saved
                    attract_saved = None
            else:
                if attract_saved is None:
                    attract_saved = (index, mode)
                previous_index = index
                index, mode = snapshot.index, snapshot.view_mode
                if index != previous_index:
                    focus_ms = 0
                if snapshot.phase is AttractPhase.SETTLED:
                    preview = PreviewPlayback(time_ms=snapshot.phase_elapsed_ms)

            #: Set the instant any input dismisses attract mode this frame --
            #: by a direct KEYDOWN/JOYBUTTONDOWN below, or by a genuine
            #: navigation step (including stick motion past the deadzone,
            #: which never raises either of those) after the event loop.
            #: Every command this frame's events would otherwise have
            #: produced (a launch, an exit, a view or direction change) is
            #: discarded instead of applied, so the very input that wakes
            #: the gallery back up is spent purely on that and never doubles
            #: as a second, separate command -- in particular, never as an
            #: EXIT.
            attract_cancelled = False

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
                    self._disarmed_exit_keys.discard(event.key)
                    self._observed_down_exit_keys.discard(event.key)
                    if event.key in KEY_DIRECTIONS:
                        self.navigation.release_key(KEY_DIRECTIONS[event.key])
                elif event.type == pygame.JOYBUTTONUP:
                    pair = (event.instance_id, event.button)
                    self._disarmed_exit_buttons.discard(pair)
                    self._observed_down_exit_buttons.discard(pair)

                if event.type in (pygame.KEYDOWN, pygame.JOYBUTTONDOWN):
                    # Every key/button press resets the idle clock -- not
                    # only while attract is already running -- so a visitor
                    # steadily pressing buttons during ordinary browsing
                    # never drifts into attract mid-session; see
                    # :meth:`~launcher.attract.AttractController.notice_input`.
                    if self._attract.notice_input():
                        if attract_saved is not None:
                            index, mode = attract_saved
                            attract_saved = None
                        preview = None
                        attract_cancelled = True
                        continue

                if command is None:
                    continue
                if attract_cancelled:
                    continue
                if command is Command.EXIT:
                    # Direct event evidence that this source is down right
                    # now -- recorded before the suppression check purely as
                    # bookkeeping for _sync_exit_arming's fallback (see its
                    # docstring); it never affects whether *this* command
                    # fires.
                    if event.type == pygame.KEYDOWN:
                        self._observed_down_exit_keys.add(event.key)
                    elif event.type == pygame.JOYBUTTONDOWN:
                        self._observed_down_exit_buttons.add(
                            (event.instance_id, event.button)
                        )
                    if self._exit_is_suppressed(event):
                        _log.debug("ignoring exit: not yet armed")
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
                    else:
                        toast = self._refusal(entry, status, elapsed_ms)
                    notice = None

            steps = self.navigation.poll(elapsed_ms)
            if steps:
                # Stick movement past the deadzone (or a held direction key's
                # repeat) is genuine input too -- the cabinet's own joystick
                # navigates entirely through axis motion, which never raises
                # a KEYDOWN/JOYBUTTONDOWN for the check above to see. This is
                # what keeps a drifting stick -- which never clears the
                # deadzone and so never produces a step -- from suppressing
                # attract forever, while a real push resets the idle clock
                # and, if attract had already triggered, dismisses it exactly
                # like any other input: consumed as a wake-up, not applied.
                if self._attract.notice_input():
                    if attract_saved is not None:
                        index, mode = attract_saved
                        attract_saved = None
                    preview = None
                    attract_cancelled = True
            if steps and not attract_cancelled:
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
                preview=preview,
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

    def _attract_navigate(
        self, mode: ViewMode, index: int, count: int, direction: Direction
    ) -> int:
        """Advance one attract-mode step, via the real view's own navigation.

        The seam :class:`~launcher.attract.AttractController` calls instead
        of ever inventing its own stepping rule -- see the module docstring
        on :mod:`launcher.attract`.
        """
        return self.renderer.view(mode).navigate(index, count, direction)

    def _attract_eligible_indices(self) -> tuple[int, ...]:
        """Manifest indices attract mode may actually settle on.

        A coming-soon card, or a launchable game that has not shipped
        ``assets/preview/`` yet, has nothing to animate for the whole dwell
        period -- settling on one would look like the demo had frozen, not
        like a showcase. Restricted to entries that are launchable,
        currently reported playable (not still syncing or unavailable), and
        carry a decoded, usable preview (see
        :class:`~launcher.ui.preview.PreviewLibrary`, already decode-once
        cached, so checking this every frame costs a dict lookup).
        """
        eligible: list[int] = []
        for index, entry in enumerate(self.manifest):
            if not entry.launchable:
                continue
            if not self._status(entry.id).is_playable:
                continue
            if self.renderer.ctx.previews.get(entry) is None:
                continue
            eligible.append(index)
        return tuple(eligible)

    def _exit_is_suppressed(self, event: pygame.event.Event) -> bool:
        """Whether an EXIT command must be ignored -- see item 1.

        Arming, not timing, decides this: a key or button that was already
        down when the session opened -- or whose release has not yet been
        positively observed since -- is disarmed, and an EXIT command from a
        disarmed source is ignored no matter how long it has been held. That
        covers a press spanning the transition regardless of how long the
        hold lasts, including one that outlasts a fixed settle window (a slow
        window-focus change, or a slow-to-exit child process). Once a source
        is armed (see :meth:`_sync_exit_arming` and the KEYUP/JOYBUTTONUP
        handling in the loop), EXIT from it is immediate: a genuine press is
        never delayed or swallowed.
        """
        if event.type == pygame.KEYDOWN:
            return event.key in self._disarmed_exit_keys
        if event.type == pygame.JOYBUTTONDOWN:
            return (event.instance_id, event.button) in self._disarmed_exit_buttons
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

    def _refusal(self, entry: GameEntry, status: GameStatus, now_ms: int) -> Toast:
        """Explain why a card did not launch. Criterion E6 -- never launches."""
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
