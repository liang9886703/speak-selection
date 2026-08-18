"""选中文字自动朗读 —— 菜单栏常驻 App。

运行：python3 main.py
退出：菜单栏 🗣️ → 退出（或 Ctrl+C）
"""

import faulthandler
faulthandler.enable(all_threads=True)

import queue
import threading
import time

import rumps
from AppKit import (
    NSEvent,
    NSEventMaskLeftMouseDown,
    NSEventMaskLeftMouseUp,
    NSEventMaskLeftMouseDragged,
    NSEventMaskKeyDown,
    NSEventTypeLeftMouseDown,
    NSEventTypeLeftMouseUp,
    NSEventTypeLeftMouseDragged,
    NSEventModifierFlagCommand,
)
from PyObjCTools import AppHelper

import selection
import autostart
from speaker import Speaker, RATES
from hotkey import HotkeyListener, HOTKEY_LABEL

# 取词前的稳定延迟（等选区/复制就绪）
_READ_DELAY = 0.12
# 单次朗读最大字符数，过长截断，避免长篇卡住
_MAX_CHARS = 5000
# 复制朗读：1s 内复制 ≥2 次，则第 2 次起视为“停止/打断”朗读
_RAPID_COPY_WINDOW = 1.0
# 按下复制/剪切键后，等待系统写入剪贴板再判定的延迟
_COPY_SETTLE_DELAY = 0.15
# c / x 的虚拟键码（US 布局）
_KEYCODE_C = 8
_KEYCODE_X = 7

# 复制图片/富文本时剪贴板里常见的不可见占位符（对象替换符、零宽字符等），
# 需要剔除后再判空，否则会把“非文本”误当成文本朗读。
_INVISIBLE_CHARS = "￼﻿​‌‍⁠\xa0"


def _readable_text(value) -> str:
    """把剪贴板字符串规整为“真正可读的文本”；无有效内容时返回 ""。"""
    if not value:
        return ""
    text = str(value)
    for ch in _INVISIBLE_CHARS:
        text = text.replace(ch, "")
    text = text.strip()
    # 去掉所有空白后，若不含任何字母/数字/CJK 等可读字符，视为非文本（图片等）
    if not any(c.isalnum() for c in text):
        return ""
    return text


class SpeakSelectionApp(rumps.App):
    def __init__(self):
        super().__init__("SpeakSelection", title="🗣️", quit_button="退出")

        self._speaker = Speaker(rate="normal")
        self._paused = False
        self._dragged = False
        self._last_text = ""
        self._last_ts = 0.0
        self._last_copy_ts = 0.0  # 复制朗读模式下，上一次检测到复制的时刻
        self._mode = "copy"  # "selection" 选中朗读 / "copy" 复制朗读（默认）
        self._pb_change = None  # 复制模式下跟踪剪贴板变更计数
        self._oneshot_timers = []  # 持有一次性定时器，避免被 GC
        # 单槽队列：保证只有一个后台线程读剪贴板（NSPasteboard 非线程安全）
        self._work_q = queue.Queue(maxsize=1)

        # ---- 菜单 ----
        self._pause_item = rumps.MenuItem("暂停自动朗读", callback=self._toggle_pause)
        self._stop_item = rumps.MenuItem("停止当前朗读", callback=self._stop_speaking)

        self._mode_items = {}
        for key, label in (("selection", "选中朗读"), ("copy", "复制朗读")):
            item = rumps.MenuItem(label, callback=self._make_mode_cb(key))
            item.state = 1 if key == self._mode else 0
            self._mode_items[key] = item

        self._rate_items = {}
        for key, label in (("slow", "慢"), ("normal", "中"), ("fast", "快")):
            item = rumps.MenuItem(label, callback=self._make_rate_cb(key))
            item.state = 1 if key == "normal" else 0
            self._rate_items[key] = item

        self._autostart_item = rumps.MenuItem(
            "开机自启", callback=self._toggle_autostart
        )
        self._autostart_item.state = 1 if autostart.is_enabled() else 0

        self.menu = [
            self._pause_item,
            self._stop_item,
            {"触发模式": list(self._mode_items.values())},
            {"语速": list(self._rate_items.values())},
            None,  # 分隔线
            self._autostart_item,
        ]

    # ---------- 启动后安装监听（在主线程 / 主 run loop） ----------
    def install_listeners(self):
        self._check_permission()

        # 唯一的取词工作线程（串行，避免并发访问剪贴板崩溃）
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

        # 复制朗读模式：主线程轮询剪贴板变更
        self._pb_timer = rumps.Timer(self._poll_clipboard, 0.4)
        self._pb_timer.start()

        # 必须持有 monitor 对象，否则回调可能被释放导致崩溃
        self._mouse_monitor = (
            NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskLeftMouseDown
                | NSEventMaskLeftMouseUp
                | NSEventMaskLeftMouseDragged,
                self._on_mouse_event,
            )
        )

        self._hotkey = HotkeyListener(self._toggle_pause_from_hotkey)
        if not self._hotkey.start():
            print("⚠️  暂停热键未启用（缺少辅助功能权限）")

        # 复制朗读模式：监听 Cmd+C / Cmd+X 按键本身，
        # 以便「按了复制键但没选中内容」也能等同暂停（停止当前朗读）。
        self._key_monitor = (
            NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskKeyDown, self._on_key_event
            )
        )

    def _after(self, delay, fn):
        """在主 run loop 上延迟执行一次 fn（NSPasteboard 需在主线程访问）。"""

        def wrapped(timer):
            timer.stop()
            try:
                self._oneshot_timers.remove(timer)
            except ValueError:
                pass
            fn()

        t = rumps.Timer(wrapped, delay)
        self._oneshot_timers.append(t)
        t.start()

    def _check_permission(self):
        try:
            from ApplicationServices import (
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )

            trusted = AXIsProcessTrustedWithOptions(
                {kAXTrustedCheckOptionPrompt: True}
            )
        except Exception:
            from ApplicationServices import AXIsProcessTrusted

            trusted = AXIsProcessTrusted()

        if not trusted:
            print(
                "⚠️  尚未授予辅助功能权限。\n"
                "   请在「系统设置 → 隐私与安全性 → 辅助功能」中，勾选运行本程序的\n"
                "   终端 / Python 解释器，然后重启本程序。"
            )

    # ---------- 鼠标事件 ----------
    def _on_mouse_event(self, event):
        etype = event.type()
        if etype == NSEventTypeLeftMouseDown:
            self._dragged = False
        elif etype == NSEventTypeLeftMouseDragged:
            self._dragged = True
        elif etype == NSEventTypeLeftMouseUp:
            if self._paused or self._mode != "selection":
                return
            # 只对「拖拽选择」或「双击/三击选词选行」取词
            qualifies = self._dragged or event.clickCount() >= 2
            if qualifies:
                # 投递给唯一的工作线程；若已有待处理任务则合并丢弃
                try:
                    self._work_q.put_nowait(True)
                except queue.Full:
                    pass

    def _worker_loop(self):
        while True:
            self._work_q.get()  # 阻塞等待下一次选择事件
            try:
                self._process_selection()
            except Exception as e:
                print("选择处理出错:", e)

    def _process_selection(self):
        time.sleep(_READ_DELAY)
        # 拖拽/多击手势才允许剪贴板回退，避免误触时刷屏
        text = selection.get_selected_text(allow_clipboard=True)
        if not text:
            return
        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS]

        now = time.monotonic()
        # 去重：同一段文字 1.5s 内不重复朗读（仅本线程访问，无需加锁）
        if text == self._last_text and (now - self._last_ts) < 1.5:
            return
        self._last_text = text
        self._last_ts = now

        # AVSpeechSynthesizer 回到主线程调用
        AppHelper.callAfter(self._speaker.speak, text)

    # ---------- 复制朗读 ----------
    def _on_key_event(self, event):
        """复制模式下监听 Cmd+C / Cmd+X 按键本身。

        目的：即使「按了复制键但没选中内容」（剪贴板不变），也能等同暂停。
        全局 monitor 回调运行在主线程，可安全读 NSPasteboard。
        """
        if self._mode != "copy" or self._paused:
            return
        if not (event.modifierFlags() & NSEventModifierFlagCommand):
            return
        if event.keyCode() not in (_KEYCODE_C, _KEYCODE_X):
            return
        from AppKit import NSPasteboard

        prev_cc = NSPasteboard.generalPasteboard().changeCount()
        # 等系统把内容写入剪贴板后再判定
        self._after(_COPY_SETTLE_DELAY, lambda: self._after_copy(prev_cc))

    def _after_copy(self, prev_cc):
        if self._mode != "copy" or self._paused:
            return
        from AppKit import NSPasteboard

        pb = NSPasteboard.generalPasteboard()
        cc = pb.changeCount()
        if self._pb_change is None:
            self._pb_change = prev_cc
        if cc == prev_cc:
            # 按了复制/剪切，但剪贴板没变 → 没选中任何内容 → 等同暂停
            self._speaker.stop()
            return
        self._consume_clipboard(pb, cc, cc - self._pb_change)

    def _poll_clipboard(self, _timer):
        """兜底：捕捉非快捷键来源的复制（右键菜单、程序化复制等）。"""
        if self._mode != "copy" or self._paused:
            return
        from AppKit import NSPasteboard

        pb = NSPasteboard.generalPasteboard()
        cc = pb.changeCount()
        # 首次进入复制模式时先建立基线，不朗读已有内容
        if self._pb_change is None:
            self._pb_change = cc
            return
        if cc == self._pb_change:
            return
        self._consume_clipboard(pb, cc, cc - self._pb_change)

    def _consume_clipboard(self, pb, cc, delta):
        """处理一次剪贴板变更：有可读文本则朗读，空/非文本则等同暂停。

        delta：本次相对上次已处理的 changeCount 增量，>=2 说明短时间内多次复制。
        """
        from AppKit import NSPasteboardTypeString

        self._pb_change = cc  # 标记已处理，避免键盘路径与轮询重复朗读

        text = _readable_text(pb.stringForType_(NSPasteboardTypeString))
        if not text:
            # 复制/剪切内容为空（或图片等非文本）→ 等同暂停：停止当前朗读。
            self._speaker.stop()
            return
        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS]

        now = time.monotonic()
        # 快速连续复制（1s 内 ≥2 次）：第 2 次起视为“停止/打断”，不再朗读新内容。
        rapid = delta >= 2 or (now - self._last_copy_ts) < _RAPID_COPY_WINDOW
        self._last_copy_ts = now
        if rapid:
            self._speaker.stop()
            return

        if text == self._last_text and (now - self._last_ts) < 1.5:
            return
        self._last_text = text
        self._last_ts = now
        self._speaker.speak(text)

    # ---------- 菜单回调 ----------
    def _make_mode_cb(self, key):
        def cb(_sender=None):
            self._mode = key
            for k, item in self._mode_items.items():
                item.state = 1 if k == key else 0
            if key == "copy":
                # 重新建立剪贴板基线，避免朗读切换前的旧内容
                self._pb_change = None
            self._notify("触发模式：" + ("复制朗读" if key == "copy" else "选中朗读"))

        return cb

    def _toggle_pause(self, _sender=None):
        self._paused = not self._paused
        self._pause_item.state = 1 if self._paused else 0
        if self._paused:
            self._speaker.stop()

    def _toggle_pause_from_hotkey(self):
        # 热键回调在主 run loop 线程，直接切换即可
        self._toggle_pause()
        self._notify(f"自动朗读：{'已暂停' if self._paused else '已开启'}")

    def _stop_speaking(self, _sender=None):
        self._speaker.stop()

    def _make_rate_cb(self, key):
        def cb(_sender=None):
            self._speaker.set_rate(key)
            for k, item in self._rate_items.items():
                item.state = 1 if k == key else 0

        return cb

    def _toggle_autostart(self, _sender=None):
        if autostart.is_enabled():
            autostart.disable()
            self._autostart_item.state = 0
            self._notify("已关闭开机自启")
        else:
            autostart.enable()
            self._autostart_item.state = 1
            self._notify("已开启开机自启")

    def _notify(self, msg):
        try:
            rumps.notification("选中朗读", "", msg)
        except Exception:
            print(msg)


def main():
    app = SpeakSelectionApp()
    # rumps 启动后、run loop 就绪时安装监听
    rumps.Timer(lambda _t: (_t.stop(), app.install_listeners()), 0.1).start()
    app.run()


if __name__ == "__main__":
    main()
