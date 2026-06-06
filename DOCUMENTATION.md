# Claude Code Buddy — 文件清单

ESP32 (M5StickC Plus) 桌面宠物固件 + 桌面端集成工具。

## 目录结构总览

```
claude-code-buddy/
├── claude_code_buddy.ino   # Arduino 主程序 (Entry Point)
├── buddy.h / buddy.cpp     # ASCII 宠物渲染引擎
├── character.h / character.cpp  # GIF 角色渲染引擎
├── ble_bridge.h / ble_bridge.cpp  # BLE 通信层 (Nordic UART Service)
├── data.h                  # 数据模型 + JSON 协议解析
├── xfer.h                  # 文件传输 (LittleFS 写入)
├── stats.h                 # 持久化统计 (NVS)
├── buddies/                # 18 种 ASCII 宠物物种
├── hooks/                  # Claude Code CLI 集成
├── tools/                  # 开发/测试工具脚本
└── README.md               # 项目说明
```

---

## 核心固件文件

### `claude_code_buddy.ino` — Arduino 主程序
**用途**: ESP32 设备的入口文件，包含 `setup()` 和 `loop()` 主循环。
- 初始化 M5StickCPlus (屏幕、IMU、按键、蜂鸣器、轴特电源管理)
- 启动 BLE 服务，设备名从蓝牙 MAC 派生 (`Claude-XXXX`)
- 主循环处理: 状态派生、按钮交互、显示渲染、IMU 休眠检测、自动熄屏、LED 控制、时钟模式
- 支持 4 种显示模式: 正常 (PET/INFO)、信息面板、宠物统计、菜单
- 翻面休眠 (IMU 检测)、30 秒无操作熄屏、USB 充电时自动切换为时钟面

### `buddy.h` / `buddy.cpp` — ASCII 宠物渲染引擎
**用途**: 在 135×240 屏幕上渲染 ASCII 风格宠物动画。
- `buddyInit()` / `buddyTick()` / `buddyInvalidate()`: 生命周期管理，200ms tick (5fps)
- 18 种预设 ASCII 物种 (水豚、猫、龙、章鱼、企鹅...), 每种 7 种情绪状态
- `buddyRenderTo()`: 支持重定向到任意 TFT_eSPI 表面 (用于横屏时钟模式)
- 缩放支持: 正常 2×、Peek 模式 1×
- 物种切换通过 NVS 持久化
- 渲染优化: tick 门控避免每帧重复绘制 (~12× 性能提升)

### `character.h` / `character.cpp` — GIF 角色渲染引擎
**用途**: 渲染从 LittleFS 加载的自定义 GIF 角色包。
- `characterInit()`: 挂载 LittleFS，解析 `/characters/<name>/manifest.json`
- 支持两种模式:
  - **GIF 模式**: 使用 AnimatedGIF 库逐帧解码，支持透明度
  - **Text 模式**: 纯文本帧动画 (manifest 中 `"mode":"text"`)
- `characterSetState()`: 根据 7 种情绪状态切换对应 GIF
- 多变体循环: 同一状态可配置多个 GIF 轮流播放
- Peek 模式: 在信息面板中半尺寸渲染
- 内存优化: 单 GIF 状态停止循环避免 flash 磨损和多 RTOS 任务饥饿

### `ble_bridge.h` / `ble_bridge.cpp` — BLE 通信层
**用途**: 实现 Nordic UART Service (NUS), 让桌面端通过 BLE 与 ESP32 通信。
- Service UUID: `6e400001-b5a3-f393-e0a9-e50e24dcca9e`
- RX Character: `6e400002-...` (桌面 → ESP32, WRITE)
- TX Character: `6e400003-...` (ESP32 → 桌面, NOTIFY)
- 环形缓冲区接收数据，chunk 分片发送 (适应 negotiated MTU)
- LE Secure Connections 配对: passkey-entry 模式 (6 位数字在屏幕上显示)
- MTU 协商 (请求 517, macOS 通常协商到 185)
- 自动重连: 断开后重启广播
- Bond 管理: `bleClearBonds()` 清除所有配对

### `data.h` — 数据模型 + JSON 协议解析
**用途**: 定义宠物状态数据结构，解析来自 USB/BLE 的 JSON 数据。
- `TamaState`: 核心状态结构体 (会话数、tokens、消息、审批请求等)
- `_applyJson()`: 解析 JSON 并更新状态
  - `{"time": [...]}`: 时间同步 → 设置 ESP32 RTC
  - 状态更新: total/running/waiting/completed/tokens/msg/entries/prompt
- 三种显示模式 (优先级排序):
  - **demo**: 自动循环假场景 (每 8s)
  - **live**: 真实 USB/BT 数据 (10s 内有数据)
  - **asleep**: 无数据，显示 "No Claude connected"
- 行缓冲区 (_LineBuf<1024>): 分别处理 USB Serial 和 BLE 输入，按行 dispatch

### `xfer.h` — 文件传输协议
**用途**: 通过 JSON-over-BLE 将 GIF 角色包传输到 ESP32 的 LittleFS。
- 命令协议 (通过 `xferCommand()` 分发):
  - `"name"` / `"owner"`: 设置宠物/主人名字
  - `"species"`: 切换 ASCII 物种
  - `"unpair"`: 清除所有 BLE 配对
  - `"status"`: 返回完整设备状态 (电池、系统、统计)
  - `"char_begin"`: 开始传输角色包 (含空间检查)
  - `"file"`: 创建文件
  - `"chunk"`: Base64 编码的数据块 (每块 ~256 字节)
  - `"file_end"` / `"char_end"`: 结束传输，重新加载角色
- 使用 mbedtls 进行 Base64 解码
- 每次 chunk 都发送 ack (因为 LittleFS write 可能阻塞，防止 UART 溢出)
- 安装新角色时自动清除旧角色以释放空间

### `stats.h` — 持久化统计 (NVS)
**用途**: 通过 ESP32 Preferences 库将统计数据写入 NVS，断电不丢失。
- 统计数据结构:
  - `napSeconds`: 累计翻面休眠时间
  - `approvals` / `denials`: 审批/拒绝次数
  - `velocity[8]`: 环形缓冲区，记录每次审批的响应时间 (秒)
  - `level`: 等级 (由 tokens 驱动, 每 50K tokens 升一级)
  - `tokens`: 累计输出 tokens
- 保存策略: 只在关键事件时写入 (审批、拒绝、休眠结束), 避免 NVS 磨损 (~100K 周期)
- Mood 计算: 基于响应速度和审批/拒绝比率 (0-4 级)
- 能量系统: 休眠满充满 5 格，每 2 小时消耗 1 格
- 喂饱进度: 每级 10 格，显示在宠物统计界面
- Settings 存储: sound/bt/wifi/led/hud/clockRotation
- 宠物/主人名字: 带 JSON-safe 过滤

---

## ASCII 宠物物种 (`buddies/`)

共 18 种 ASCII 宠物，每种一个 `.cpp` 文件，定义 7 种情绪状态的动画帧。

| 文件 | 宠物 | 说明 |
|------|------|------|
| `buddies/axolotl.cpp` | 六角恐龙 | 水栖小怪物 |
| `buddies/blob.cpp` | 史莱姆 | 简单的 blob 形态 |
| `buddies/cactus.cpp` | 仙人掌 | 带刺的盆栽朋友 |
| `buddies/capybara.cpp` | 水豚 | 最佛生的水豚 |
| `buddies/cat.cpp` | 猫 | 5 种睡姿 (蜷缩、呼吸、尾巴抖动、打呼、做梦) |
| `buddies/chonk.cpp` | Chonk | 圆滚滚的小家伙 |
| `buddies/dragon.cpp` | 龙 | 喷火小龙 |
| `buddies/duck.cpp` | 鸭子 | 嘎嘎叫的鸭子 |
| `buddies/ghost.cpp` | 幽灵 | 飘来飘去的小幽灵 |
| `buddies/goose.cpp` | 鹅 | 鹅就是鹅 |
| `buddies/mushroom.cpp` | 蘑菇 | 小蘑菇 |
| `buddies/octopus.cpp` | 章鱼 | 八爪鱼 |
| `buddies/owl.cpp` | 猫头鹰 | 熬夜的猫头鹰 |
| `buddies/penguin.cpp` | 企鹅 | 摇摇晃晃的企鹅 |
| `buddies/rabbit.cpp` | 兔子 | 蹦蹦跳跳的兔子 |
| `buddies/robot.cpp` | 机器人 | 未来战士 |
| `buddies/snail.cpp` | 蜗牛 | 慢慢爬的蜗牛 |
| `buddies/turtle.cpp` | 乌龟 | 坚持不懈的乌龟 |

每种物种的实现模式一致: 定义一个 `Species` 结构体，包含名称、身体颜色、以及 7 个状态函数指针。每个状态函数接收全局 tickCount，使用 `buddyPrintSprite` / `buddyPrintLine` 等公共渲染 API 绘制到精灵缓冲区。

---

## 桌面端集成 (`hooks/`)

### `hooks/buddy_hook.py` — Claude Code Hook 脚本
**用途**: Claude Code CLI 的 hook 入口，通过 Unix socket 与 buddyd 守护进程通信。
- **零第三方依赖** (仅使用 Python 标准库)
- 从 stdin 读取 Claude Code 的 hook 事件 JSON
- 事件映射:
  - `SessionStart` / `SessionEnd`: 会话开始/结束
  - `UserPromptSubmit`: 用户提交 prompt
  - `PreToolUse`: 工具执行前 (分两种处理)
    - 只读工具 (Read/Glob/Grep/TaskList/TaskGet): 仅更新状态，不触发审批
    - 写工具: 发送权限请求，等待按钮响应
  - `PostToolUse` / `PostToolUseFailure`: 工具执行结果
  - `Notification` / `PermissionRequest`: 通知/权限事件
- 读取环境变量: `BUDDY_SOCK` (socket 路径), `BUDDY_HOOK_LOG` (可选日志)

### `hooks/buddyd.py` — BLE 守护进程
**用途**: 后台常驻进程，维护 ESP32 的持久 BLE 连接，接收 hook 命令并转发。
- 依赖: `bleak` (BLE 异步库)
- 核心功能:
  - **BLE 扫描连接**: 自动发现 `Claude-XXXX` 设备
  - **Unix socket 服务器**: 监听 `~/.claude/buddy.sock` 接收 hook 命令
  - **状态转发**: 将 hook 的状态更新通过 BLE NUS TX 发送到 ESP32
  - **权限审批**: 发送 prompt → 等待 ESP32 按钮响应 (25s 超时) → 返回结果给 hook
  - **心跳保活**: 每 10s 发送时间同步，维持 ESP32 的 dataConnected() 计时器
  - **自动重连**: 断开后 3s 重试
- 协议: Nordic UART Service (NUS), JSON over BLE
- 守护进程模式: fork 后台运行，PID 文件管理，signal 处理
- 日志: 写入 `~/.claude/buddyd.log`

### `hooks/buddyctl.sh` — 守护进程管理脚本
**用途**: 命令行工具，管理 buddyd 守护进程的生命周期。
- `start`: 后台启动守护进程
- `stop`: 优雅停止 (5s 后强制 kill)
- `restart`: 停止 + 启动
- `status`: 查看运行状态 + BLE 连接状态 (通过 ping 命令)
- `log`: tail -f 实时日志

### `hooks/install_hooks.py` — Hook 自动安装器
**用途**: 自动在 `~/.claude/settings.json` 中注册 6 个 hook 事件绑定。
- 注册事件: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, Notification
- `--uninstall`: 移除已安装的 hooks
- `--project`: 安装到项目级 `.claude/settings.json` 而非全局
- 防重复安装: 检测是否已存在相同的 command 条目

### `hooks/com.claude.buddyd.plist` — macOS launchd 配置
**用途**: macOS 系统级自动启动配置。
- Label: `com.claude.buddyd`
- RunAtLoad + KeepAlive: 开机自启 + 崩溃后自动重启
- 需手动替换 `/path/to/` 为实际路径后安装到 `~/Library/LaunchAgents/`

---

## 工具脚本 (`tools/`)

### `tools/prep_character.py` — 角色包预处理
**用途**: 将原始 GIF 素材转换为 ESP32 可用的角色包格式。
- 输入: 包含 `manifest.json` 的角色目录 或 ZIP 文件
- 跨状态统一裁剪: 计算所有状态帧的全局 bbox，确保角色在每种情绪下大小一致
- 缩放: 归一化到 1000px 宽 → 计算 bbox → 输出到设备 96px 宽
- 格式转换: RGBA GIF → 64 色索引模式 (节省空间)
- 生成: `characters/<name>/manifest.json` + 各状态 GIF 文件
- 体积检查: 超过 1800KB 时告警，提示使用 gifsicle 压缩

### `tools/flash_character.py` — 角色包烧录
**用途**: 将预处理好的角色包烧录到 ESP32 的 LittleFS 分区。
- 通过 `pio run -t uploadfs` (PlatformIO) 烧录文件系统镜像
- 烧录前空间检查 (最大 1.8MB)
- 自动替换 `data/characters/` 下的内容
- 烧录完成后提示用户在设备上切换到 GIF 模式

### `tools/test_serial.py` — 串口通信测试
**用途**: 快速验证 ESP32 对 JSON 状态更新的响应。
- 自动检测 `/dev/cu.usbserial-*` 串口
- 每 3s 循环发送 4 种假状态 (sleep → idle → busy → attention)
- 观察屏幕上宠物动画是否随之切换

### `tools/test_xfer.py` — 文件传输协议测试
**用途**: 通过串口完整测试角色包的 BLE 文件传输协议。
- 发送 `char_begin` → 逐文件发送 (`file` + `chunk` + `file_end`) → `char_end`
- 每个命令等待 ESP32 的 ack 响应
- 显示传输速度和文件大小
- 测试完整的安装流程 (不依赖 BLE 链路)

---

## 通信架构

```
Claude Code CLI
  └─ Hook (buddy_hook.py) — 标准库, 无第三方依赖
       └─ Unix Socket (~/.claude/buddy.sock)
            └─ buddyd.py — bleak BLE 守护进程
                 └─ BLE Nordic UART (NUS)
                      └─ ESP32 (M5StickC Plus)
                           ├── ASCII 宠物 (buddy)
                           └─ GIF 角色 (character)
                           └─ 按钮审批 (A=允许, B=拒绝)
```
