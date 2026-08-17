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
    NSEventTypeLeftMouseDown,
    NSEventTypeLeftMouseUp,
    NSEventTypeLeftMouseDragged,
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


class SpeakSelectionApp(rumps.App):
    def __init__(self):
        super().__init__("SpeakSelection", title="🗣️", quit_button="退出")

        self._speaker = Speaker(rate="normal")
        self._paused = False
        self._dragged = False
        self._last_text = ""
        self._last_ts = 0.0
        self._last_copy_ts = 0.0  # 复制朗读模式下，上一次检测到复制的时刻
        self._mode = "selection"  # "selection" 选中朗读 / "copy" 复制朗读
        self._pb_change = None  # 复制模式下跟踪剪贴板变更计数
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

    # ---------- 复制朗读：主线程轮询剪贴板 ----------
    def _poll_clipboard(self, _timer):
        if self._mode != "copy" or self._paused:
            return
        from AppKit import NSPasteboard, NSPasteboardTypeString

        pb = NSPasteboard.generalPasteboard()
        cc = pb.changeCount()
        # 首次进入复制模式时先建立基线，不朗读已有内容
        if self._pb_change is None:
            self._pb_change = cc
            return
        if cc == self._pb_change:
            return
        # changeCount 自上次轮询以来的增量：>=2 说明两次复制落在同一个轮询间隔内
        delta = cc - self._pb_change
        self._pb_change = cc

        now = time.monotonic()
        # 快速连续复制（1s 内 ≥2 次）：第 2 次起视为“停止/打断”当前朗读，不再朗读新内容。
        # delta>=2 覆盖两次复制同处一个轮询窗口的情况；否则按距上次复制的间隔判定。
        rapid = delta >= 2 or (now - self._last_copy_ts) < _RAPID_COPY_WINDOW
        self._last_copy_ts = now
        if rapid:
            self._speaker.stop()
            return

        value = pb.stringForType_(NSPasteboardTypeString)
        text = (str(value).strip() if value else "")
        if not text:
            return
        if len(text) > _MAX_CHARS:
            text = text[:_MAX_CHARS]

        now = time.monotonic()
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
