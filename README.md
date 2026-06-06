# Claude Code Buddy

一个 ESP32 桌面宠物，通过 BLE 与 Claude Code CLI 终端集成。在你的桌面上展示一个有情绪的小精灵，实时反映你的 Claude 会话状态，还可以通过物理按钮审批工具执行权限。

基于 [claude-desktop-buddy](https://github.com/nicepkg/claude-desktop-buddy) 改造，专为 Claude Code CLI 设计。

## 硬件要求

- **M5StickC Plus** (ESP32) — 自带 135×240 TFT 屏幕、IMU、按钮、电池
- USB-C 数据线（用于供电和烧录）

## 功能

- **7 种情绪状态**: Sleep, Idle, Busy, Attention, Celebrate, Dizzy, Heart
- **实时状态同步**: 显示 Claude 当前正在做什么（编辑文件、运行命令等）
- **物理按钮审批**: 在 ESP32 上按 Button A 允许 / Button B 拒绝工具执行
- **18 种 ASCII 宠物**: 猫、鸭子、龙、章鱼、水豚...
- **GIF 动画角色**: 支持自定义 GIF 角色包
- **持久化统计**: 审批/拒绝次数、等级、心情
- **自动休眠**: 30 秒无交互自动关屏，翻面休眠

## 快速开始

### 1. 安装依赖

```bash
pip install bleak
```

### 2. 烧录固件（Arduino IDE）

1. 安装 [Arduino IDE 2.x](https://www.arduino.cc/en/software)
2. 添加 M5Stack 板子支持：
   - `文件 → 首选项 → 附加开发板管理器 URLs` 添加：
   ```
   https://m5stack.arduino.cc/static/json/m5stack.json
   ```
3. 安装库（`工具 → 管理库`）：
   - **M5StickCPlus** (M5Stack)
   - **ArduinoJson** (Benoit Blanchon, v7.x)
   - **AnimatedGIF** (Larry Bank)
4. 选择开发板：`工具 → 开发板 → M5Stack → M5Stick-C`
5. 分区方案：`工具 → Partition Scheme → No OTA (Large APP)`
6. 打开 `claude_code_buddy.ino`，点击上传

### 3. 启动 BLE 守护进程

```bash
cd hooks
./buddyctl.sh start
```

守护进程会自动扫描并连接 ESP32（设备名 `Claude-XXXX`）。

### 4. 配置 Claude Code Hooks

```bash
cd hooks
python3 install_hooks.py
```

这会自动在 `~/.claude/settings.json` 中添加 6 个 hook 事件绑定。

### 5. 重启 Claude Code

完成！宠物会在你使用 Claude Code 时实时响应。

## 目录结构

```
claude-code-buddy/
  claude_code_buddy.ino    # Arduino IDE 主程序
  buddy.h / buddy.cpp      # ASCII 物种渲染
  character.h / character.cpp  # GIF 角色渲染
  ble_bridge.h / ble_bridge.cpp  # BLE 通信（Nordic UART Service）
  data.h                   # 协议解析、连接状态
  stats.h                  # 持久化统计（NVS）
  xfer.h                   # 文件传输
  buddies/                 # 18 个 ASCII 物种
  hooks/                   # Claude Code 集成
    buddyd.py              # BLE 守护进程（后台运行）
    buddy_hook.py          # Claude Code Hook 脚本
    buddyctl.sh            # 守护进程管理（start/stop/status/log）
    install_hooks.py       # 自动配置 hooks
    com.claude.buddyd.plist # macOS launchd 自动启动配置
  tools/                   # 工具脚本
```

## 架构

```
Claude Code CLI
  └─ Hooks (settings.json)
       └─ buddy_hook.py (轻量脚本，无第三方依赖)
            ├─ 连接 Unix socket (~/.claude/buddy.sock)
            ├─ 信息类事件: 发送状态 → 立即返回
            └─ PreToolUse: 发送权限请求 → 等待按钮响应 → 返回 decision

buddyd.py (后台守护进程)
  ├─ bleak 维持 BLE 长连接到 ESP32
  ├─ 监听 Unix socket 接收 hook 命令
  ├─ 转发状态 JSON → ESP32 (Nordic UART Service)
  ├─ 接收 ESP32 按钮响应 ← (BLE notify)
  ├─ 自动重连（断连后 3s 重试）
  └─ 心跳保活（每 10s 时间同步）

ESP32 (M5StickC Plus)
  ├─ BLE Nordic UART Service
  ├─ 接收 JSON → 更新状态 → 渲染宠物动画
  └─ Button A = 允许 / Button B = 拒绝
```

## 权限审批流程

1. Claude Code 准备执行工具（如 `Bash: rm -rf /tmp/foo`）
2. PreToolUse hook 触发 → `buddy_hook.py` 发送权限请求到守护进程
3. 守护进程通过 BLE 转发到 ESP32 → 屏幕显示审批界面 + LED 闪烁
4. 你在 ESP32 上按 Button A（允许）或 Button B（拒绝）
5. ESP32 通过 BLE 返回决定 → hook 脚本返回给 Claude Code
6. **超时 25 秒未响应** → 自动降级到 Claude Code 终端 UI

> 注意：只读工具（Read、Glob、Grep 等）不会触发物理审批，直接通过。

## 守护进程管理

```bash
./buddyctl.sh start     # 启动守护进程
./buddyctl.sh stop      # 停止
./buddyctl.sh restart   # 重启
./buddyctl.sh status    # 查看状态和 BLE 连接
./buddyctl.sh log       # 查看实时日志
```

### macOS 开机自启

```bash
# 编辑 hooks/com.claude.buddyd.plist，将 /path/to/ 替换为实际路径
sed -i '' "s|/path/to/|$(pwd)/|g" hooks/com.claude.buddyd.plist

# 安装
cp hooks/com.claude.buddyd.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.claude.buddyd.plist
```

## 测试

```bash
# 测试 hook 脚本（不需要 Claude Code 运行）
echo '{"hook_event_name":"SessionStart"}' | python3 hooks/buddy_hook.py
echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"ls"}}' | python3 hooks/buddy_hook.py

# 测试串口通信（USB 连接）
python3 tools/test_serial.py

# 查看守护进程日志
./buddyctl.sh log
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `BUDDY_SOCK` | Unix socket 路径 | `~/.claude/buddy.sock` |
| `BUDDY_HOOK_LOG` | Hook 脚本日志路径 | 无（不记录） |
| `BUDDY_LOG` | 守护进程日志路径 | `~/.claude/buddyd.log` |

## 卸载

```bash
# 移除 hooks
python3 hooks/install_hooks.py --uninstall

# 停止守护进程
./buddyctl.sh stop

# 移除 launchd（如果安装了）
launchctl unload ~/Library/LaunchAgents/com.claude.buddyd.plist
rm ~/Library/LaunchAgents/com.claude.buddyd.plist
```

## 致谢

- 原项目: [claude-desktop-buddy](https://github.com/nicepkg/claude-desktop-buddy)
- BLE 库: [bleak](https://github.com/hbldh/bleak)
- 硬件: [M5StickC Plus](https://docs.m5stack.com/en/core/m5stickc_plus)
