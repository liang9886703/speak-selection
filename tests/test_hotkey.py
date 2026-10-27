"""Double-tap behavior without installing event taps or posting system events."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hotkey
from hotkey import Quartz as Q

LEFT = Q.kCGEventFlagMaskAlternate | hotkey._LEFT_OPTION_FLAG
RIGHT = Q.kCGEventFlagMaskAlternate | hotkey._RIGHT_OPTION_FLAG


class DoubleTapTests(unittest.TestCase):
    def setUp(self):
        self.gesture = hotkey._LeftOptionDoubleTap()

    def tap(self, at, duration=0.05, key=58, flags=LEFT, released_flags=0):
        self.assertFalse(self.gesture.flags_changed(key, flags, at))
        return self.gesture.flags_changed(key, released_flags, at + duration)

    def test_fires_only_on_second_release(self):
        self.assertFalse(self.tap(1))
        self.assertFalse(self.gesture.flags_changed(58, LEFT, 1.15))
        self.assertTrue(self.gesture.flags_changed(58, 0, 1.20))

    def test_single_tap_does_not_fire(self):
        self.assertFalse(self.tap(1))

    def test_slow_pair_does_not_fire_but_next_fast_tap_does(self):
        self.assertFalse(self.tap(1))
        self.assertFalse(self.tap(2))
        self.assertTrue(self.tap(2.15))

    def test_long_press_cancels_pair(self):
        self.tap(1)
        self.assertFalse(self.tap(1.15, duration=0.5))
        self.assertFalse(self.tap(1.8))

    def test_right_option_never_fires(self):
        self.assertFalse(self.tap(1, key=61, flags=RIGHT))
        self.assertFalse(self.tap(1.15, key=61, flags=RIGHT))

    def test_switching_sides_is_not_double_tap(self):
        self.tap(1)
        self.assertFalse(self.tap(1.15, key=61, flags=RIGHT))
        self.assertFalse(self.tap(1.3))

    def test_other_modifiers_cancel(self):
        for other in (Q.kCGEventFlagMaskCommand, Q.kCGEventFlagMaskControl,
                      Q.kCGEventFlagMaskShift, Q.kCGEventFlagMaskSecondaryFn):
            with self.subTest(modifier=other):
                self.gesture.reset()
                self.tap(1)
                self.assertFalse(self.tap(1.15, flags=LEFT | other))
                self.assertFalse(self.tap(1.3))

    def test_both_options_are_rejected(self):
        self.tap(1)
        self.assertFalse(self.tap(1.15, flags=LEFT | RIGHT))
        self.assertFalse(self.tap(1.3))

    def test_caps_lock_does_not_block_double_tap(self):
        caps = Q.kCGEventFlagMaskAlphaShift
        self.assertFalse(self.tap(1, flags=LEFT | caps, released_flags=caps))
        self.assertTrue(self.tap(1.15, flags=LEFT | caps, released_flags=caps))

    def test_triple_tap_fires_once(self):
        self.assertFalse(self.tap(1))
        self.assertTrue(self.tap(1.15))
        self.assertFalse(self.tap(1.3))

    def test_orphan_release_does_not_count(self):
        self.assertFalse(self.gesture.flags_changed(58, 0, 1))
        self.assertFalse(self.tap(1.15))


class ListenerTests(unittest.TestCase):
    def setUp(self):
        self.read = Mock()
        self.toggle = Mock()
        self.listener = hotkey.HotkeyListener(self.toggle, self.read)
        self.mocks = {}
        for name, value in (
            ('CGEventTapCreate', object()), ('CFMachPortCreateRunLoopSource', object()),
            ('CFRunLoopGetCurrent', object()), ('CFRunLoopAddSource', None),
            ('CGEventTapEnable', None),
        ):
            p = patch.object(Q, name, return_value=value)
            self.mocks[name] = p.start()
            self.addCleanup(p.stop)
        for name, side_effect in (
            ('CGEventGetIntegerValueField', lambda e, f: e.repeat if f == Q.kCGKeyboardEventAutorepeat else e.key),
            ('CGEventGetFlags', lambda e: e.flags),
            ('CGEventGetTimestamp', lambda e: int(e.at * 1_000_000_000)),
        ):
            p = patch.object(Q, name, side_effect=side_effect)
            p.start()
            self.addCleanup(p.stop)
        self.assertTrue(self.listener.start())

    def send(self, kind, at=1, key=58, flags=0, repeat=0):
        event = SimpleNamespace(at=at, key=key, flags=flags, repeat=repeat)
        result = self.listener._callback(None, kind, event, None)
        self.assertIs(result, event)

    def tap(self, at):
        self.send(Q.kCGEventFlagsChanged, at, flags=LEFT)
        self.send(Q.kCGEventFlagsChanged, at + 0.05)

    def test_left_double_tap_invokes_read_once_and_preserves_events(self):
        self.tap(1)
        self.tap(1.15)
        self.read.assert_called_once_with()
        self.toggle.assert_not_called()

    def test_typing_between_taps_cancels(self):
        self.tap(1)
        self.send(Q.kCGEventKeyDown, 1.10, key=0)
        self.send(Q.kCGEventKeyUp, 1.12, key=0)
        self.tap(1.15)
        self.read.assert_not_called()

    def test_option_letter_combo_cancels(self):
        self.send(Q.kCGEventFlagsChanged, 1, flags=LEFT)
        self.send(Q.kCGEventKeyDown, 1.02, key=0, flags=LEFT)
        self.send(Q.kCGEventKeyUp, 1.04, key=0, flags=LEFT)
        self.send(Q.kCGEventFlagsChanged, 1.06)
        self.tap(1.15)
        self.read.assert_not_called()

    def test_held_letter_blocks_double_tap(self):
        self.send(Q.kCGEventKeyDown, 0.9, key=0)
        self.tap(1)
        self.tap(1.15)
        self.read.assert_not_called()
        self.send(Q.kCGEventKeyUp, 1.3, key=0)
        self.tap(1.4)
        self.tap(1.55)
        self.read.assert_called_once()

    def test_mouse_click_between_taps_cancels(self):
        self.tap(1)
        self.send(Q.kCGEventLeftMouseDown, 1.1)
        self.send(Q.kCGEventLeftMouseUp, 1.12)
        self.tap(1.15)
        self.read.assert_not_called()

    def test_held_mouse_blocks_until_released(self):
        self.send(Q.kCGEventLeftMouseDown, 0.9)
        self.tap(1)
        self.tap(1.15)
        self.read.assert_not_called()
        self.send(Q.kCGEventLeftMouseUp, 1.3)
        self.tap(1.4)
        self.tap(1.55)
        self.read.assert_called_once()

    def test_scroll_between_taps_cancels(self):
        self.tap(1)
        self.send(Q.kCGEventScrollWheel, 1.1)
        self.tap(1.15)
        self.read.assert_not_called()

    def test_pause_is_preserved_and_autorepeat_ignored(self):
        self.send(Q.kCGEventKeyDown, key=1, flags=hotkey._REQUIRED_FLAGS)
        self.send(Q.kCGEventKeyDown, key=1, flags=hotkey._REQUIRED_FLAGS, repeat=1)
        self.toggle.assert_called_once_with()
        self.read.assert_not_called()

    def test_copy_cut_and_old_shortcut_do_not_read(self):
        for key in (8, 7, 37):
            self.send(Q.kCGEventKeyDown, key=key, flags=hotkey._REQUIRED_FLAGS)
            self.send(Q.kCGEventKeyUp, key=key)
        self.read.assert_not_called()

    def test_tap_reenable_clears_partial_gesture(self):
        self.tap(1)
        self.send(Q.kCGEventTapDisabledByTimeout, 1.1)
        self.tap(1.15)
        self.read.assert_not_called()
        self.mocks['CGEventTapEnable'].assert_called_with(self.listener._tap, True)

    def test_event_mask_captures_gestures_and_is_listen_only(self):
        args = self.mocks['CGEventTapCreate'].call_args.args
        self.assertEqual(args[2], Q.kCGEventTapOptionListenOnly)
        for kind in (Q.kCGEventFlagsChanged, Q.kCGEventKeyDown, Q.kCGEventKeyUp,
                     Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp, Q.kCGEventScrollWheel):
            self.assertTrue(args[3] & Q.CGEventMaskBit(kind))

    def test_missing_permission_returns_false(self):
        self.mocks['CGEventTapCreate'].return_value = None
        self.assertFalse(self.listener.start())


if __name__ == '__main__':
    unittest.main()
