"""Input state: joystick debouncing, auto-repeat, and hot-plugging.

These are the rules that stop a single flick of the arcade stick from skipping
three games. They are pure logic with an injected clock -- no SDL, no hardware.
"""

from __future__ import annotations

import unittest

from launcher.controls import (
    AXIS_HORIZONTAL,
    AXIS_VERTICAL,
    BUTTON_CYCLE_VIEW,
    BUTTON_EXIT,
    BUTTON_LAUNCH,
    Command,
    command_for_button,
)
from launcher.input_state import (
    AxisAggregator,
    Direction,
    DirectionRepeater,
    NavigationController,
    RepeatPolicy,
)

POLICY = RepeatPolicy(initial_delay_ms=380, repeat_ms=140)


class DebounceTests(unittest.TestCase):
    """One push must produce exactly one step, however long the frame is."""

    def setUp(self) -> None:
        self.repeater = DirectionRepeater(POLICY)

    def test_push_fires_once_immediately(self) -> None:
        self.assertEqual(self.repeater.update([Direction.RIGHT], 0), (Direction.RIGHT,))

    def test_holding_does_not_repeat_before_the_initial_delay(self) -> None:
        self.repeater.update([Direction.RIGHT], 0)
        for now in range(16, POLICY.initial_delay_ms, 16):
            with self.subTest(now=now):
                self.assertEqual(self.repeater.update([Direction.RIGHT], now), ())

    def test_a_single_flick_produces_a_single_step(self) -> None:
        """The regression this whole module exists to prevent."""
        steps = 0
        for now in range(0, 200, 16):  # ~200 ms of holding, then release
            steps += len(self.repeater.update([Direction.RIGHT], now))
        steps += len(self.repeater.update([], 208))
        self.assertEqual(steps, 1)

    def test_repeat_starts_after_the_initial_delay(self) -> None:
        self.repeater.update([Direction.RIGHT], 0)
        self.assertEqual(
            self.repeater.update([Direction.RIGHT], POLICY.initial_delay_ms),
            (Direction.RIGHT,),
        )

    def test_repeat_interval_is_respected(self) -> None:
        self.repeater.update([Direction.RIGHT], 0)
        self.repeater.update([Direction.RIGHT], POLICY.initial_delay_ms)
        half = POLICY.initial_delay_ms + POLICY.repeat_ms // 2
        self.assertEqual(self.repeater.update([Direction.RIGHT], half), ())
        full = POLICY.initial_delay_ms + POLICY.repeat_ms
        self.assertEqual(self.repeater.update([Direction.RIGHT], full), (Direction.RIGHT,))

    def test_release_then_push_fires_immediately_again(self) -> None:
        self.repeater.update([Direction.RIGHT], 0)
        self.repeater.update([], 50)
        self.assertEqual(self.repeater.update([Direction.RIGHT], 60), (Direction.RIGHT,))

    def test_two_second_hold_repeats_a_sane_number_of_times(self) -> None:
        total = 0
        for now in range(0, 2000, 16):
            total += len(self.repeater.update([Direction.RIGHT], now))
        # 1 immediate + roughly (2000-380)/140 repeats; never a runaway scroll.
        self.assertGreaterEqual(total, 8)
        self.assertLessEqual(total, 14)

    def test_directions_have_independent_timers(self) -> None:
        self.repeater.update([Direction.RIGHT], 0)
        fired = self.repeater.update([Direction.RIGHT, Direction.DOWN], 100)
        self.assertEqual(fired, (Direction.DOWN,))

    def test_reset_forgets_holds(self) -> None:
        self.repeater.update([Direction.RIGHT], 0)
        self.repeater.reset()
        self.assertEqual(self.repeater.update([Direction.RIGHT], 10), (Direction.RIGHT,))

    def test_invalid_policies_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RepeatPolicy(initial_delay_ms=-1)
        with self.assertRaises(ValueError):
            RepeatPolicy(repeat_ms=0)


class AxisTests(unittest.TestCase):
    """The cabinet's stick is digital; 0.5 is the documented threshold."""

    def setUp(self) -> None:
        self.axes = AxisAggregator(deadzone=0.5)
        self.axes.attach(0)

    def test_below_deadzone_is_not_a_direction(self) -> None:
        self.axes.set_axis(0, AXIS_HORIZONTAL, 0.49)
        self.assertEqual(self.axes.held_directions(), frozenset())

    def test_at_deadzone_counts(self) -> None:
        self.axes.set_axis(0, AXIS_HORIZONTAL, 0.5)
        self.assertEqual(self.axes.held_directions(), frozenset({Direction.RIGHT}))

    def test_axis_orientation_matches_the_cabinet(self) -> None:
        self.axes.set_axis(0, AXIS_VERTICAL, -1.0)
        self.assertIn(Direction.UP, self.axes.held_directions())
        self.axes.set_axis(0, AXIS_VERTICAL, 1.0)
        self.assertIn(Direction.DOWN, self.axes.held_directions())

    def test_diagonals_are_reported(self) -> None:
        self.axes.set_axis(0, AXIS_HORIZONTAL, -1.0)
        self.axes.set_axis(0, AXIS_VERTICAL, 1.0)
        self.assertEqual(
            self.axes.held_directions(), frozenset({Direction.LEFT, Direction.DOWN})
        )

    def test_detaching_a_stick_drops_its_direction(self) -> None:
        self.axes.set_axis(0, AXIS_HORIZONTAL, 1.0)
        self.axes.detach(0)
        self.assertEqual(self.axes.held_directions(), frozenset())

    def test_unknown_device_is_attached_on_the_fly(self) -> None:
        self.axes.set_axis(7, AXIS_HORIZONTAL, 1.0)
        self.assertIn(7, self.axes.device_ids)

    def test_two_sticks_are_merged(self) -> None:
        self.axes.set_axis(0, AXIS_HORIZONTAL, 1.0)
        self.axes.set_axis(1, AXIS_VERTICAL, -1.0)
        self.assertEqual(
            self.axes.held_directions(), frozenset({Direction.RIGHT, Direction.UP})
        )

    def test_invalid_deadzone_is_rejected(self) -> None:
        for bad in (0.0, 1.0, -0.2, 3.0):
            with self.subTest(deadzone=bad), self.assertRaises(ValueError):
                AxisAggregator(deadzone=bad)


class NavigationControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.navigation = NavigationController.from_policy(POLICY, 0.5)

    def test_keyboard_and_stick_share_one_repeat_policy(self) -> None:
        self.navigation.press_key(Direction.LEFT)
        self.assertEqual(self.navigation.poll(0), (Direction.LEFT,))
        self.assertEqual(self.navigation.poll(100), ())

    def test_stick_push_is_debounced_like_a_key(self) -> None:
        self.navigation.axes.set_axis(3, AXIS_HORIZONTAL, 1.0)
        self.assertEqual(self.navigation.poll(0), (Direction.RIGHT,))
        self.assertEqual(self.navigation.poll(120), ())

    def test_reset_clears_everything(self) -> None:
        self.navigation.press_key(Direction.UP)
        self.navigation.axes.set_axis(0, AXIS_VERTICAL, 1.0)
        self.navigation.reset()
        self.assertEqual(self.navigation.poll(500), ())


class ButtonMappingTests(unittest.TestCase):
    """Button ids follow the arcade's documented map (criterion E1)."""

    def test_documented_button_numbers(self) -> None:
        self.assertEqual((BUTTON_LAUNCH, BUTTON_EXIT, BUTTON_CYCLE_VIEW), (1, 5, 8))

    def test_integer_button_ids(self) -> None:
        self.assertIs(command_for_button(BUTTON_LAUNCH), Command.LAUNCH)
        self.assertIs(command_for_button(BUTTON_EXIT), Command.EXIT)
        self.assertIs(command_for_button(BUTTON_CYCLE_VIEW), Command.CYCLE_VIEW)

    def test_string_button_ids(self) -> None:
        """The arcade's own joystick.py hands buttons over as strings."""
        self.assertIs(command_for_button("5"), Command.EXIT)
        self.assertIs(command_for_button("1"), Command.LAUNCH)

    def test_unmapped_buttons_do_nothing(self) -> None:
        for button in (0, 2, 3, 4, 9, 42, "nonsense", None):
            with self.subTest(button=button):
                self.assertIsNone(command_for_button(button))

    def test_insert_money_is_deliberately_inert(self) -> None:
        self.assertIsNone(command_for_button(4))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
