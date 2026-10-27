"""取词：优先 Accessibility API，取不到再回退模拟 Cmd+C 读剪贴板。

对外只暴露一个函数：get_selected_text(allow_clipboard: bool) -> str
返回去掉首尾空白后的选中文本；无选区时返回 ""。
"""

import time

from AppKit import NSPasteboard
from ApplicationServices import (
    AXUIElementCreateSystemWide,
    AXUIElementCopyAttributeValue,
    kAXFocusedUIElementAttribute,
    kAXSelectedTextAttribute,
)
import Quartz

# 'c' 的虚拟键码（US 布局），配合 Command flag 即 Cmd+C
_KEYCODE_C = 8


def _ax_selected_text() -> str:
    """通过辅助功能 API 读取当前聚焦元素的选中文本；无副作用，不动剪贴板。"""
    system = AXUIElementCreateSystemWide()
    err, focused = AXUIElementCopyAttributeValue(
        system, kAXFocusedUIElementAttribute, None
    )
    if err != 0 or focused is None:
        return ""
    err, value = AXUIElementCopyAttributeValue(
        focused, kAXSelectedTextAttribute, None
    )
    if err != 0 or not value:
        return ""
    return str(value)


def _post_cmd_c() -> None:
    """合成一次 Cmd+C 按下/抬起事件。"""
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)

    key_down = Quartz.CGEventCreateKeyboardEvent(src, _KEYCODE_C, True)
    Quartz.CGEventSetFlags(key_down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, key_down)

    key_up = Quartz.CGEventCreateKeyboardEvent(src, _KEYCODE_C, False)
    Quartz.CGEventSetFlags(key_up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, key_up)


def _snapshot_pasteboard(pb) -> dict:
    """备份剪贴板当前所有类型的数据，便于之后恢复。"""
    saved = {}
    types = pb.types() or []
    for t in types:
        data = pb.dataForType_(t)
        if data is not None:
            saved[t] = data
    return saved


def _restore_pasteboard(pb, saved: dict) -> None:
    pb.clearContents()
    if not saved:
        return
    pb.declareTypes_owner_(list(saved.keys()), None)
    for t, data in saved.items():
        pb.setData_forType_(data, t)


def _clipboard_selected_text() -> str:
    """回退方案：备份剪贴板 → 模拟 Cmd+C → 读取 → 恢复剪贴板。"""
    pb = NSPasteboard.generalPasteboard()
    saved = _snapshot_pasteboard(pb)
    pb.clearContents()
    prev_change = pb.changeCount()
    _post_cmd_c()

    # 轮询等待复制写入（最多 ~0.35s），比固定 sleep 更稳。
    text = ""
    for _ in range(7):
        time.sleep(0.05)
        if pb.changeCount() != prev_change:
            from AppKit import NSPasteboardTypeString

            value = pb.stringForType_(NSPasteboardTypeString)
            if value:
                text = str(value)
            break

    _restore_pasteboard(pb, saved)
    return text


def get_selected_text(allow_clipboard: bool = True) -> str:
    """获取当前选中文字。

    先尝试无副作用的 AX 读取；取不到且允许时再回退剪贴板复制。
    """
    text = _ax_selected_text()
    if text.strip():
        return text.strip()

    if allow_clipboard:
        text = _clipboard_selected_text()
        if text.strip():
            return text.strip()

    return ""
