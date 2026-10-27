"""双击左 Option 朗读选区，⌃⌥⌘S 暂停/恢复。

使用只监听的 CGEventTap，不拦截普通按键；需要辅助功能权限。
"""

import time

import Quartz

_KEYCODE_S = 1
_KEYCODE_LEFT_OPTION = 58
# IOKit/hidsystem/IOLLEvent.h 中的 NX_DEVICELALTKEYMASK / NX_DEVICERALTKEYMASK。
_LEFT_OPTION_FLAG = 0x20
_RIGHT_OPTION_FLAG = 0x40
_REQUIRED_FLAGS = (
    Quartz.kCGEventFlagMaskControl
    | Quartz.kCGEventFlagMaskAlternate
    | Quartz.kCGEventFlagMaskCommand
)
_FLAG_MASK = _REQUIRED_FLAGS | Quartz.kCGEventFlagMaskShift
_GESTURE_FLAG_MASK = _FLAG_MASK | Quartz.kCGEventFlagMaskSecondaryFn
_DOUBLE_TAP_INTERVAL = 0.4  # 两次松开之间的最长间隔（秒）
_MAX_TAP_DURATION = 0.3  # 每次必须是轻按，长按不计入双击

HOTKEY_LABEL = "⌃⌥⌘S"
READ_HOTKEY_LABEL = "双击左 Option"


class _LeftOptionDoubleTap:
    def __init__(self):
        self.reset()

    def reset(self):
        self._pressed_at = None
        self._released_at = None

    def flags_changed(self, keycode, flags, now) -> bool:
        """只接受两次独立的左 Option 按下/松开，在第二次松开时触发。"""
        if keycode != _KEYCODE_LEFT_OPTION:
            self.reset()
            return False

        modifiers = flags & _GESTURE_FLAG_MASK
        left_only = (
            modifiers == Quartz.kCGEventFlagMaskAlternate
            and flags & _LEFT_OPTION_FLAG
            and not flags & _RIGHT_OPTION_FLAG
        )
        if left_only:
            if self._pressed_at is None:
                self._pressed_at = now
            return False

        if modifiers == 0 and self._pressed_at is not None:
            duration = now - self._pressed_at
            self._pressed_at = None
            if not 0 <= duration <= _MAX_TAP_DURATION:
                self.reset()
                return False
            if (
                self._released_at is not None
                and 0 <= now - self._released_at <= _DOUBLE_TAP_INTERVAL
            ):
                self.reset()
                return True
            self._released_at = now
            return False

        self.reset()
        return False


class HotkeyListener:
    def __init__(self, on_toggle, on_read=None):
        self._on_toggle = on_toggle
        self._on_read = on_read
        self._tap = None
        self._source = None
        self._callback = None  # 持有回调，防止被 GC 回收
        self._double_tap = _LeftOptionDoubleTap()
        self._keys_down = set()
        self._buttons_down = set()

    def start(self) -> bool:
        mouse_down = {
            Quartz.kCGEventLeftMouseDown: 0,
            Quartz.kCGEventRightMouseDown: 1,
            Quartz.kCGEventOtherMouseDown: 2,
        }
        mouse_up = {
            Quartz.kCGEventLeftMouseUp: 0,
            Quartz.kCGEventRightMouseUp: 1,
            Quartz.kCGEventOtherMouseUp: 2,
        }

        def callback(proxy, type_, event, refcon):
            if type_ in (
                Quartz.kCGEventTapDisabledByTimeout,
                Quartz.kCGEventTapDisabledByUserInput,
            ):
                self._double_tap.reset()
                self._keys_down.clear()
                self._buttons_down.clear()
                Quartz.CGEventTapEnable(self._tap, True)
                return event

            action = None
            if type_ == Quartz.kCGEventFlagsChanged:
                if self._keys_down or self._buttons_down:
                    self._double_tap.reset()
                elif self._double_tap.flags_changed(
                    Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode),
                    Quartz.CGEventGetFlags(event),
                    Quartz.CGEventGetTimestamp(event) / 1_000_000_000,
                ):
                    action = self._on_read
            else:
                # 任何中间的普通按键、鼠标按键或滚轮操作，都取消本次双击。
                self._double_tap.reset()
                if type_ in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                    keycode = Quartz.CGEventGetIntegerValueField(
                        event, Quartz.kCGKeyboardEventKeycode
                    )
                    if type_ == Quartz.kCGEventKeyDown:
                        self._keys_down.add(keycode)
                        flags = Quartz.CGEventGetFlags(event) & _FLAG_MASK
                        repeat = Quartz.CGEventGetIntegerValueField(
                            event, Quartz.kCGKeyboardEventAutorepeat
                        )
                        if keycode == _KEYCODE_S and flags == _REQUIRED_FLAGS and not repeat:
                            action = self._on_toggle
                    else:
                        self._keys_down.discard(keycode)
                elif type_ in mouse_down:
                    self._buttons_down.add(mouse_down[type_])
                elif type_ in mouse_up:
                    self._buttons_down.discard(mouse_up[type_])

            if action is not None:
                try:
                    action()
                except Exception as exc:
                    print("热键处理出错:", exc)
            return event

        self._callback = callback
        event_types = (
            Quartz.kCGEventFlagsChanged,
            Quartz.kCGEventKeyDown,
            Quartz.kCGEventKeyUp,
            Quartz.kCGEventScrollWheel,
            *mouse_down,
            *mouse_up,
        )
        mask = 0
        for event_type in event_types:
            mask |= Quartz.CGEventMaskBit(event_type)
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            callback,
            None,
        )
        if self._tap is None:
            return False

        self._source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        Quartz.CFRunLoopAddSource(
            Quartz.CFRunLoopGetCurrent(), self._source, Quartz.kCFRunLoopCommonModes
        )
        Quartz.CGEventTapEnable(self._tap, True)
        return True

    def wait_for_release(self, timeout=3.0) -> bool:
        """在取词线程等待物理修饰键松开；超时则取消本次朗读。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            flags = Quartz.CGEventSourceFlagsState(
                Quartz.kCGEventSourceStateCombinedSessionState
            )
            if not flags & _FLAG_MASK:
                return True
            time.sleep(0.02)
        return False
