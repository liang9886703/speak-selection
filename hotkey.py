"""全局暂停/恢复热键：默认 ⌃⌥⌘S，通过 CGEventTap 监听（listen-only）。

需要辅助功能权限。安装成功返回 True；未授权时 CGEventTapCreate 返回 None。
"""

import Quartz

# 's' 的虚拟键码（US 布局）
_KEYCODE_S = 1

# 需要同时按下的修饰键：Control + Option + Command
_REQUIRED_FLAGS = (
    Quartz.kCGEventFlagMaskControl
    | Quartz.kCGEventFlagMaskAlternate
    | Quartz.kCGEventFlagMaskCommand
)

# 忽略这些无关位（大小写锁、数字小键盘等），只比较我们关心的修饰键
_FLAG_MASK = (
    Quartz.kCGEventFlagMaskControl
    | Quartz.kCGEventFlagMaskAlternate
    | Quartz.kCGEventFlagMaskCommand
    | Quartz.kCGEventFlagMaskShift
)

HOTKEY_LABEL = "⌃⌥⌘S"


class HotkeyListener:
    def __init__(self, on_toggle):
        self._on_toggle = on_toggle
        self._tap = None
        self._source = None
        self._callback = None  # 持有回调，防止被 GC 回收导致崩溃

    def start(self) -> bool:
        def callback(proxy, type_, event, refcon):
            if type_ == Quartz.kCGEventKeyDown:
                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode
                )
                flags = Quartz.CGEventGetFlags(event) & _FLAG_MASK
                if keycode == _KEYCODE_S and flags == _REQUIRED_FLAGS:
                    try:
                        self._on_toggle()
                    except Exception:
                        pass
            return event

        self._callback = callback
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown),
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
