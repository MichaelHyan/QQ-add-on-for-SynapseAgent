# QQ-add-on-for-SynapseAgent

把 **SynapseAgent** 接入 QQ 官方机器人（QQ 开放平台 API v2）的连接器。

两种回复模式，通过 `config.yaml` 的 `reply_mode` 切换：

| 模式 | 说明 | 用途 |
| --- | --- | --- |
| `fixed` | 收到消息回固定文本 | 验证 QQ 链路是否通 |
| `agent` | 转发给后端 SynapseAgent，周期轮询取回复 | 正式使用 |

---

## 一、功能现状

| 能力 | 状态 |
| --- | --- |
| `access_token` 获取与自动刷新（提前 60s） | ✅ |
| WebSocket 网关接入（Hello / Identify / Resume） | ✅ |
| 心跳保活（ACK 超时自动断线重连） | ✅ |
| 断线指数退避重连 | ✅ |
| 群聊 @机器人 / 单聊 / 频道 / 频道私信 收发 | ✅ |
| 固定回复模式 | ✅ |
| 后端 Agent 模式（ask 投递 + answer 轮询） | ✅ |
| 分段回复合并（避免刷屏 + 规避 msg_seq 上限） | ✅ |
| 工具调用标签过滤 | ✅ |
| 图片/音频/视频附件 base64 编码 | ✅ |
| 首响应宽限期 + 总超时双保护 | ✅ |
| 消息去重容错（40054005 降级为 warning） | ✅ |
| WebHook 接入方式 | ⏳ 未实现（当前用 WebSocket，本地无需公网域名） |

---

## 二、目录结构

```
QQ-add-on-for-SynapseAgent/
├── main.py                  # 主入口：装配双模式并启动
├── check.py                 # 自检：验证凭证 + 网关地址（不建长连接）
├── config.example.yaml      # 配置模板
├── config.yaml              # 你的实际配置（已在 .gitignore）
├── requirements.txt
├── start.bat                # 双击启动（已处理中文编码）
├── qqbot/
│   ├── logger.py            # 日志：控制台 + logs/qqbot.log
│   ├── config.py            # 配置加载（YAML + 环境变量）
│   ├── auth.py              # access_token 获取与刷新
│   ├── api.py               # OpenAPI 封装（网关、消息发送）
│   ├── gateway.py           # WebSocket 网关客户端
│   ├── handler.py           # 事件解析 + 回复分发（双模式）
│   ├── agent_client.py      # 后端 Agent HTTP 客户端（解析 result 嵌套结构）
│   ├── bootstrap.py         # 启动时写入 prompt.md 到后端系统提示词
│   ├── media.py             # 附件下载 + base64 编码（image/audio/video）
│   └── bridge.py            # 消息队列 + 投递/轮询 + 合并窗口
└── tests/
    ├── offline_check.py         # 事件解析与分发（6 项）
    ├── agent_client_check.py    # Agent 响应解析（8 项）
    ├── agent_bridge_check.py    # 桥接链路（16 项）
    ├── bootstrap_check.py       # 提示词初始化（6 项）
    └── media_check.py           # 媒体附件处理（13 项）
```

---

## 三、快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动后端 Agent（agent 模式必需）

```bash
cd D:\git\SynapseAgent
python server.py
```

默认监听 `http://127.0.0.1:56700/cnmd`。

验证一下：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:56700/cnmd" -Method Post `
  -ContentType "application/json" -Body '{"method":"ping","params":{}}'
```

返回 `{"ok":true,...}` 即通。

### 3. 准备配置

```bash
copy config.example.yaml config.yaml
```

填入 AppID / AppSecret，并确认 `reply_mode`：

```yaml
app_id: "你的AppID"
app_secret: "你的AppSecret"
reply_mode: "agent"
agent_endpoint: "http://127.0.0.1:56700/cnmd"
```

也可用环境变量（优先级更高）：

```bash
set QQ_BOT_APP_ID=你的AppID
set QQ_BOT_APP_SECRET=你的AppSecret
set QQ_BOT_REPLY_MODE=agent
```

### 4. 自检（推荐先跑）

只验证凭证与网关可用性，不建立长连接：

```bash
python check.py
```

### 5. 启动机器人

```bash
python main.py
```

或直接双击 `start.bat`。

上线成功日志：

```
鉴权成功：session_id=xxx 机器人=突触助理(...)
回复模式：Agent（endpoint=http://127.0.0.1:56700/cnmd 轮询=3.0s 首响应宽限=60s 总超时=300s 队列=32）
```

### 6. 离线自测（无需凭证、无需 Agent）

```bash
python -m tests.offline_check        # OFFLINE_CHECK_OK 6 calls
python -m tests.agent_client_check   # AGENT_CLIENT_CHECK_OK 8 scenarios
python -m tests.agent_bridge_check   # AGENT_BRIDGE_CHECK_OK 16 scenarios
python -m tests.bootstrap_check      # BOOTSTRAP_CHECK_OK 6 scenarios
python -m tests.media_check          # MEDIA_CHECK_OK 13 scenarios
```

各模块覆盖内容：

| 模块 | 覆盖 |
| --- | --- |
| `offline_check` | 四种事件解析、msg_seq 递增、@ 清理、超长截断 |
| `agent_client_check` | `result` 嵌套解析、扁平兼容、`ok=false`、请求体结构 |
| `agent_bridge_check` | 单段/多段合并、窗口过期刷出、空 status=0 等待、宽限期耗尽、超长切分、条数上限、标签剥离、空内容拦截、四种消息类型前缀规则 |
| `bootstrap_check` | 空文件跳过、文件缺失跳过、payload 格式、残留清理、超时不崩 |
| `media_check` | 类型识别、base64 编码、超限降级、下载失败、总量预算、非媒体附件 |

---

## 四、配置项说明

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `app_id` | 机器人 AppID | 必填 |
| `app_secret` | 机器人 AppSecret | 必填 |
| `intents` | 事件订阅位掩码 | `1107296256` |
| `reply_mode` | `fixed` / `agent` | `fixed` |
| `reply_text` | 固定回复内容（fixed 模式） | 见模板 |
| `agent_endpoint` | Agent 接口地址 | `http://127.0.0.1:56700/cnmd` |
| `agent_poll_interval` | 轮询 answer 间隔（秒） | `3.0` |
| `agent_timeout` | 单次 HTTP 超时（秒） | `15.0` |
| `agent_pending_timeout` | 单条消息等待上限（秒） | `300.0` |
| `agent_first_reply_timeout` | 首次拿到内容的宽限期（秒） | `60.0` |
| `agent_queue_size` | 待投递队列长度 | `32` |
| `shard` | 分片 `[当前, 总数]` | `[0, 1]` |
| `token_urls` | access_token 地址（按序回退） | `bots.qq.com` → `api.bot.qq.com` |
| `api_bases` | OpenAPI 地址（按序回退） | `api.sgroup.qq.com` → `api.bot.qq.com` |
| `log_level` | 日志级别 | `INFO` |
| `log_dir` | 日志目录 | `logs` |

### intents 说明

```
1 << 25  群聊 @机器人 / 单聊消息（GROUP_AND_C2C_EVENT）
1 << 30  频道 @机器人（PUBLIC_GUILD_MESSAGES）
```

- 默认两者都订阅。
- 若启动后立即报"无权限/连接被关闭"，说明该事件类型未申请到权限；把 `intents` 改为 `33554432`（仅群聊/单聊）。

---

## 五、后端 Agent 协议

与 `wechat-add-on-for-SynapseAgent` 完全一致：**周期性 HTTP POST**。

| 方法 | 请求体 | 语义 |
| --- | --- | --- |
| `ping` | `{"method":"ping","params":{}}` | 连通性探测 |
| `ask` | `{"method":"ask","params":{"content":"用户消息"}}` | 投递消息，立即返回 `status:1` |
| `answer` | `{"method":"answer","params":{"type":"rp"}}` | 取回复 |

### ⚠️ answer 的状态语义（这里有个坑）

`ask` 只是把消息丢给 Agent 的**后台线程**，立即返回。Agent 需要时间初始化 + 生成。此时立刻轮询 `answer`，会拿到：

```json
{"status": 0, "response": ""}
```

**这个 `status=0` 不代表本轮结束**，只代表"Agent 还没开始产出"。

正确判定规则：

| 组合 | 含义 | 动作 |
| --- | --- | --- |
| `status=1` | 仍在生成 | 继续轮询 |
| `status=1` + 有内容 | 生成中，可先发 | 累积内容 |
| `status=0` + **有内容** | 本轮真正结束 | 发出剩余内容，释放会话 |
| `status=0` + **无内容** | 还没开始产出 | **继续等待**，受首响应宽限期约束 |

早期实现把 `status=0` 一律当作结束，导致消息刚投递 3 秒就被判定"完成"，回复永远发不出去。现在由 `agent_first_reply_timeout`（默认 60s）兜底：只要从未拿到内容且超过该时长，才判定 Agent 无响应并提示用户。

### 桥接行为

- **单会话串行**：全局只有一条消息在等 Agent 回复，后续消息排队。
- **分段合并**：Agent 可能多次返回非空片段，桥接会累积合并，避免逐段刷屏；只有累积超过 1200 字或本轮结束才发送。
- **条数上限**：QQ 对同一 `msg_id` 的 `msg_seq` 上限为 5 次，桥接最多发 4 条，超出截断并加提示。
- **标签过滤**：Agent 输出中的工具调用标签会被剥离。
- **双超时**：`agent_first_reply_timeout`（首次内容）+ `agent_pending_timeout`（总时长）。
- **断连容错**：Agent 不可达时暂停轮询，恢复后自动继续；`ask` 失败时回提示并释放会话。

---

## 六、实现要点

- **鉴权**：`POST /app/getAppAccessToken`，`Authorization: QQBot {access_token}`。token 有效期约 7200s，过期前 60s 内请求会拿到新 token。
- **被动回复**：群聊/单聊回复必须带原消息 `msg_id`，且同一条消息的 `msg_seq` 需递增（已自动维护）。
- **消息去重**：平台错误码 `40054005` 表示同一 `msg_id` + `msg_seq` 重复提交。常见原因是多实例同时在线，或事件被网关重推。已降级为 `WARNING` 并跳过，不打堆栈。
- **频道消息**：内容里的 `<@!123456>` @标记会先被清理。
- **长度限制**：单条回复超过 1800 字符自动截断。
- **不阻塞收包**：事件处理通过 `asyncio.create_task` 并发执行，避免单条消息处理慢拖垮心跳。
- **双地址回退**：`api.bot.qq.com` 与 `api.sgroup.qq.com` 都支持，失败自动切换。

---

## 七、常见问题

| 现象 | 排查方向 |
| --- | --- |
| `缺少 app_id / app_secret` | 没有 `config.yaml`，或键名写错 |
| `获取 access_token 失败` | AppID/AppSecret 错误；本机无法访问 `bots.qq.com` |
| 连接后立刻断开 | intents 无权限，参考第四节 |
| 收到消息但回复失败 | 查看日志中的 `[401]`/`[403]`；确认机器人已加入对应群/频道 |
| 群聊无响应 | 群聊需要 **@机器人** 才会下发 `GROUP_AT_MESSAGE_CREATE` |
| 频道无响应 | 需 `1 << 30` 权限；私域机器人发全量消息还需 `1 << 9` |
| Agent 模式无回复 | 确认 `python server.py` 已启动；跑 `ping` 验证；检查 `agent_first_reply_timeout` 是否过小 |
| 日志出现 `消息被去重` | 多实例同时在线，检查是否跑了多个 `main.py` |
| 只收到部分回复 | 内容超长被切分/截断，属预期行为 |
| 回复里带奇怪标签 | 已在 `bridge.py` 过滤；若 Agent 新增标签需同步扩展正则 |

日志位置：`logs/qqbot.log`

---

## 八、已知限制

**全局单会话串行**：同一时刻只有一条消息在等 Agent 回复。后果是——A 用户的任务正在生成时，B、C 用户的消息会排队等待，要等 A 完成才轮到。

这在单人使用场景（如微信机器人）没问题，但 QQ 群里多人同时说话时会明显卡顿。

如需改动，方向是「按 `target_id` 分队列并发」：每个群/用户一条独立串行队列，不同人互不阻塞，仍保持单条消息内的连续上下文。改动集中在 `bridge.py`，约 30 行。
