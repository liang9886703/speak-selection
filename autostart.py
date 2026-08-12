"""开机自启：写入/删除 LaunchAgent plist，并用 launchctl 生效。"""

import os
import subprocess

LABEL = "com.user.speak-selection"
_PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _main_py() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")


def _python() -> str:
    import sys

    return sys.executable or "/usr/bin/python3"


def is_enabled() -> bool:
    return os.path.exists(_PLIST_PATH)


def enable() -> None:
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_python()}</string>
        <string>{_main_py()}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
"""
    os.makedirs(os.path.dirname(_PLIST_PATH), exist_ok=True)
    with open(_PLIST_PATH, "w", encoding="utf-8") as f:
        f.write(plist)
    # 忽略已加载等报错
    subprocess.run(["launchctl", "unload", _PLIST_PATH],
                   capture_output=True)
    subprocess.run(["launchctl", "load", _PLIST_PATH], capture_output=True)


def disable() -> None:
    if os.path.exists(_PLIST_PATH):
        subprocess.run(["launchctl", "unload", _PLIST_PATH], capture_output=True)
        os.remove(_PLIST_PATH)
