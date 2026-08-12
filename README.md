# speak-selection

macOS 上的常驻小工具：**用鼠标选中任意文字，自动朗读出来**。

- 取词：优先 Accessibility API（无副作用），取不到再回退模拟 Cmd+C 读剪贴板并恢复。
- 朗读：`AVSpeechSynthesizer`，自动按中/英选择语音，可打断、可调语速。
- 触发：监听全局鼠标 `mouseUp`，仅对「拖拽选择」或「双击/三击选词选行」取词，配合去重与防抖。
- 常驻：菜单栏 🗣️（暂停/停止/语速/开机自启/退出）+ 全局暂停热键 `⌃⌥⌘S`。

## 安装

```bash
cd ~/code/speak-selection
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 运行

```bash
python3 main.py
```

首次运行会弹出 / 提示授予**辅助功能**权限：

> 系统设置 → 隐私与安全性 → 辅助功能

在列表里勾选**运行本程序的宿主进程**（你的终端，或虚拟环境里的 `python`），然后**重启本程序**。
授权的是宿主进程，不是脚本本身 —— AX 取词、模拟 Cmd+C、全局热键都依赖它。

## 使用

- 拖选一段文字，或双击选词 → 自动朗读。
- 菜单栏 🗣️：
  - **暂停自动朗读** / **停止当前朗读**
  - **语速**：慢 / 中 / 快
  - **开机自启**：写入 `~/Library/LaunchAgents/com.user.speak-selection.plist`
  - **退出**
- 全局热键 **⌃⌥⌘S**：随时暂停 / 恢复自动朗读。

## 已知限制

- 部分沙盒化 / 自绘文本的 App（某些 Electron、终端、游戏）AX 取不到，只能靠剪贴板回退；
  极少数连标准 Cmd+C 复制都没有的场景会失败。
- 「选中即读」是 `mouseUp` 后主动取词的启发式（macOS 无官方选区事件），
  个别边缘手势可能漏读/误读，可在 `main.py` 调 `_READ_DELAY` 或手势过滤参数。
- 开机自启由 LaunchAgent 用 `sys.executable` 拉起，确保该 Python 已在「辅助功能」中授权。

## 文件

| 文件 | 作用 |
| --- | --- |
| `main.py` | 菜单栏 App、鼠标监听、去重防抖、权限检查 |
| `selection.py` | 取词：AX 优先 + 剪贴板回退 |
| `speaker.py` | `AVSpeechSynthesizer` 封装（语言判定、语速） |
| `hotkey.py` | 全局暂停热键（CGEventTap） |
| `autostart.py` | LaunchAgent 安装 / 卸载 |
