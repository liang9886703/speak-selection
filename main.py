"""选中文字自动朗读 —— 菜单栏常驻 App。

运行：python3 main.py
退出：菜单栏 🗣️ → 退出（或 Ctrl+C）
"""

import faulthandler
faulthandler.enable(all_threads=True)

import queue
import re
import threading
import time

import rumps
from AppKit import (
    NSEvent,
    NSEventMaskLeftMouseDown,
    NSEventMaskLeftMouseUp,
    NSEventMaskLeftMouseDragged,
    NSEventTypeLeftMouseDown,
    NSEventTypeLeftMouseUp,
    NSEventTypeLeftMouseDragged,
)
from PyObjCTools import AppHelper

import selection
import autostart
from speaker import Speaker, RATES
from hotkey import HotkeyListener, HOTKEY_LABEL, READ_HOTKEY_LABEL

# 取词前的稳定延迟（等选区/复制就绪）
_READ_DELAY = 0.12
# 单次朗读最大字符数，过长截断，避免长篇卡住
_MAX_CHARS = 5000
# 超过这么多单词就不朗读（只读短词/短语）
_MAX_WORDS = 3
# 复制图片/富文本时剪贴板里常见的不可见占位符（对象替换符、零宽字符等），
# 需要剔除后再判空，否则会把“非文本”误当成文本朗读。
_INVISIBLE_CHARS = "￼﻿​‌‍⁠\xa0"


def _readable_text(value) -> str:
    """把选中文字规整为“真正可读的文本”；无有效内容时返回 ""。"""
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


_CJK = re.compile(r"[一-鿿㐀-䶿]")


def _word_count(text: str) -> int:
    """按空白分词统计单词数。"""
    return len(text.split())


def _should_skip_speech(text: str) -> bool:
    """超过 3 个单词，或含中文，则不朗读。"""
    return _word_count(text) > _MAX_WORDS or _CJK.search(text) is not None


class SpeakSelectionApp(rumps.App):
    def __init__(self):
        super().__init__("SpeakSelection", title="🗣️", quit_button="退出")

        self._speaker = Speaker(rate="normal")
        self._paused = False
        self._dragged = False
        self._last_text = ""
        self._last_ts = 0.0
        self._mode = "hotkey"  # 默认快捷键朗读，也可切换为选中自动朗读
        self._generation = 0  # 使暂停、停止或切换模式前的取词任务失效
        # 单槽队列：保证只有一个后台线程读剪贴板（NSPasteboard 非线程安全）
        self._work_q = queue.Queue(maxsize=1)

        # ---- 菜单 ----
        self._pause_item = rumps.MenuItem(f"暂停朗读（{HOTKEY_LABEL}）", callback=self._toggle_pause)
        self._stop_item = rumps.MenuItem("停止当前朗读", callback=self._stop_speaking)

        self._mode_items = {}
        for key, label in (
            ("hotkey", f"快捷键朗读（{READ_HOTKEY_LABEL}）"),
            ("selection", "选中朗读"),
        ):
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

        # 必须持有 monitor 对象，否则回调可能被释放导致崩溃
        self._mouse_monitor = (
            NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskLeftMouseDown
                | NSEventMaskLeftMouseUp
                | NSEventMaskLeftMouseDragged,
                self._on_mouse_event,
            )
        )

        self._hotkey = HotkeyListener(
            self._toggle_pause_from_hotkey, self._read_from_hotkey
        )
        if not self._hotkey.start():
            print("⚠️  全局热键未启用（缺少辅助功能权限）")

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
                self._queue_selection("selection")

    def _queue_selection(self, mode):
        # 单槽队列合并尚未开始的取词请求。
        try:
            self._work_q.put_nowait((mode, self._generation))
        except queue.Full:
            pass

    def _read_from_hotkey(self):
        if self._paused or self._mode != "hotkey":
            return
        self._queue_selection("hotkey")

    def _worker_loop(self):
        while True:
            mode, generation = self._work_q.get()
            try:
                self._process_selection(mode, generation)
            except Exception as e:
                print("选择处理出错:", e)

    def _process_selection(self, mode, generation):
        time.sleep(_READ_DELAY)
        if mode == "hotkey":
            # 等用户松开修饰键，避免剪贴板回退时干扰模拟的 Cmd+C。
            if not self._hotkey.wait_for_release():
                return
        if self._paused or self._mode != mode or self._generation != generation:
            return
        text = _readable_text(selection.get_selected_text(allow_clipboard=True))
        AppHelper.callAfter(self._speak_selection, text, mode, generation)

    def _speak_selection(self, text, mode, generation):
        # 取词期间可能暂停、停止或切换模式；失效任务不能重新开始朗读。
        if self._paused or self._mode != mode or self._generation != generation:
            return
        if not text:
            self._speaker.stop()
            return
        if _should_skip_speech(text):
            return
        text = text[:_MAX_CHARS]
        now = time.monotonic()
        # 自动选中模式去重；主动按热键允许重复朗读同一段文字。
        if mode == "selection" and text == self._last_text and now - self._last_ts < 1.5:
            return
        self._last_text = text
        self._last_ts = now
        self._speaker.speak(text)

    # ---------- 菜单回调 ----------
    def _make_mode_cb(self, key):
        def cb(_sender=None):
            self._generation += 1
            self._mode = key
            for k, item in self._mode_items.items():
                item.state = 1 if k == key else 0
            self._notify("触发模式：" + ("快捷键朗读" if key == "hotkey" else "选中朗读"))

        return cb

    def _toggle_pause(self, _sender=None):
        self._generation += 1
        self._paused = not self._paused
        self._pause_item.state = 1 if self._paused else 0
        if self._paused:
            self._speaker.stop()

    def _toggle_pause_from_hotkey(self):
        # 热键回调在主 run loop 线程，直接切换即可
        self._toggle_pause()
        self._notify(f"朗读：{'已暂停' if self._paused else '已开启'}")

    def _stop_speaking(self, _sender=None):
        self._generation += 1
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
