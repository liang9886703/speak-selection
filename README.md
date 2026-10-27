# speak-selection

macOS 上的常驻小工具：**选中文字，双击左 Option 朗读**。

- 取词：优先 Accessibility API（无副作用），取不到再回退模拟 Cmd+C 读剪贴板并恢复。
- 朗读：`AVSpeechSynthesizer`，自动按中/英选择语音，可打断、可调语速。
- 触发：默认使用全局快捷键 **双击左 Option**；也可切换为鼠标选中自动朗读。普通复制 / 剪切 / 剪贴板变化不会触发朗读。
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

- 拖选文字或双击选词，再连续轻按两次 **左 Option**（两次松开间隔不超过 0.4 秒），第二次松开后朗读；再次双击可重读，无选区时停止当前朗读。
- 单击、长按、右 Option，以及 Option 与其他键 / 鼠标的组合操作不会触发朗读。
- 默认只朗读 **不含中文且不超过 3 个空白分隔单词** 的文本。
- 普通 `⌘C` / `⌘X` 继续用于复制 / 剪切。
- 菜单栏 🗣️：
  - **暂停朗读（⌃⌥⌘S）** / **停止当前朗读**
  - **触发模式**：
    - **快捷键朗读（双击左 Option）**（默认）：选中文字后按快捷键朗读（AX 优先，回退剪贴板）
    - **选中朗读**：拖选 / 双击选词即读
  - **语速**：慢 / 中 / 快
  - **开机自启**：写入 `~/Library/LaunchAgents/com.user.speak-selection.plist`
  - **退出**
- 全局热键 **⌃⌥⌘S**：随时暂停 / 恢复朗读。

## 已知限制

- 部分沙盒化 / 自绘文本的 App（某些 Electron、终端、游戏）AX 取不到，只能靠剪贴板回退；
  极少数连标准 Cmd+C 复制都没有的场景会失败。
- 「选中即读」是 `mouseUp` 后主动取词的启发式（macOS 无官方选区事件），
  个别边缘手势可能漏读/误读，可在 `main.py` 调 `_READ_DELAY` 或手势过滤参数。
- 开机自启由 LaunchAgent 用 `sys.executable` 拉起，确保该 Python 已在「辅助功能」中授权。

## 文件

| 文件 | 作用 |
| --- | --- |
| `main.py` | 菜单栏 App、触发模式、取词队列、权限检查 |
| `selection.py` | 取词：AX 优先 + 剪贴板回退 |
| `speaker.py` | `AVSpeechSynthesizer` 封装（语言判定、语速） |
| `hotkey.py` | 全局朗读与暂停热键（CGEventTap） |
| `autostart.py` | LaunchAgent 安装 / 卸载 |
