# codex-slack

一个最小可用的 Slack 机器人桥接服务，基于 `slack_bolt` 和 `pexpect`:

- Slack 通过 Socket Mode 把消息推给本机进程
- Python 进程用 `pexpect` 调起本机 `codex exec`
- 再把 Codex 输出回发到 Slack 线程里

## 当前能力

- 频道里响应 `@机器人`
- 私聊里直接响应文本消息
- 默认用 `gpt-5.4` 调用本机 `codex exec`
- 使用 Slack 线程回消息
- 新的 Slack thread 会创建新的 Codex session
- 同一个 Slack thread 会继续复用同一个 Codex session
- 在多人白名单模式下，thread 和 attached session 都会记录 owner，避免跨用户接管
- 如果同一个 Codex session 被多个 Slack thread 绑定，服务会按 session 串行执行，避免并发 `resume`
- 支持把 Codex 长输出分片发送
- 优先只发送 Codex 的最终答复，尽量不把中间进度和思考日志发到 Slack
- 支持 `/reset`、`reset`、`reset session`、`/reset-session`、`/fresh`、`fresh`、`/session`、`session`、`/attach`、`attach`、`/where`、`where`、`/whoami`、`whoami`、`/status`、`status`、`/handoff`、`handoff`、`/recap`、`recap` 控制当前 Slack thread 的 Codex session

## 启动

1. 复制环境变量模板

```bash
cp .env.example .env
```

2. 安装依赖

```bash
pip install -r requirements.txt
```

3. 填写 `.env`

```env
OPENAI_MODEL=gpt-5.4

SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=你的_slack_signing_secret
ALLOWED_SLACK_USER_IDS=U0123456789,U0987654321
ALLOW_SHARED_ATTACH=0

CODEX_BIN=codex
CODEX_WORKDIR=/ssd/home/pz
CODEX_TIMEOUT_SECONDS=900
CODEX_SANDBOX=danger-full-access
CODEX_FULL_AUTO=0
CODEX_EXTRA_ARGS=
CODEX_SLACK_SESSION_STORE=/path/to/codex-slack/.codex-slack-sessions.json
```

说明:

- `ALLOWED_SLACK_USER_IDS` 留空表示不限制；如果填写，则只有这些 Slack `user_id` 可以使用 bot
- 多个 Slack `user_id` 用英文逗号分隔
- 获取你自己的 Slack `user_id`：在 Slack 桌面版或网页版打开个人资料，点击右上角 `⋯`，选择 `Copy member ID`
- 配白名单时要填“你的成员 ID”，不要误填 bot 自己的 `user_id`
- 系统环境变量的优先级高于 `.env` 里的同名变量；如果两边都设置了同一个 key，会优先使用进程环境变量
- `ALLOW_SHARED_ATTACH=0` 是更安全的默认值；只有在“单用户白名单”模式下，才允许把一个尚未被 bot 见过的终端 session 直接 `attach` 进来
- 如果你明确需要多用户共享 `attach`，再显式设置 `ALLOW_SHARED_ATTACH=1`
- 即使开启 `ALLOW_SHARED_ATTACH=1`，一旦某个 Slack thread 或 Codex session 已经绑定给某个 Slack 用户，其他白名单用户也不能继续或接管它
- GPU/驱动相关命令不要依赖 `--full-auto`
- 当前 `codex` 在 `--full-auto` 下可能实际退回 `workspace-write` sandbox
- 需要访问 `nvidia-smi` 这类宿主机资源时，优先显式设置 `CODEX_SANDBOX=danger-full-access`

4. 先确认本机 `codex` 已登录可用

```bash
codex exec --skip-git-repo-check "reply with exactly OK"
```

5. 启动服务

```bash
python3 server.py
```

- 只保留一个 `server.py` 进程运行；如果同时启动多个实例，它们会竞争处理 Slack 事件并覆盖同一份 session 映射文件
- 新版本启动时会持有 `.codex-slack.pid` 进程锁；如果已经有一个实例在跑，第二个实例会直接报错退出
- 如果你需要自定义 PID 锁文件位置，可以设置 `CODEX_SLACK_INSTANCE_LOCK`
- 这个 PID 锁依赖 POSIX `fcntl`，在 macOS / Linux 上有效

## Slack 配置

下面按 Slack 管理后台的实际操作顺序配置。

1. 创建 Slack App

- 打开 `https://api.slack.com/apps`
- 点击 `Create New App`
- 选择 `From scratch`
- 填写应用名，例如 `codex-slack`
- 选择目标 workspace
- 点击 `Create App`

2. 打开 Socket Mode

- 进入左侧 `Settings` -> `Socket Mode`
- 打开 `Enable Socket Mode`
- 可以填写一个连接名，例如 `codex-slack-socket`

3. 创建 App-Level Token

- 在 `Socket Mode` 页面点击 `Generate Token and Scopes`
- Token Name 可以填写 `socket-mode`
- 给这个 token 勾选 `connections:write`
- 点击 `Generate`
- 得到一个 `xapp-...` token
- 把它填进 `.env` 里的 `SLACK_APP_TOKEN`

4. 配置 Bot 权限

- 进入左侧 `Features` -> `OAuth & Permissions`
- 在 `Bot Token Scopes` 下添加这些 scope:

- `app_mentions:read`
- `chat:write`
- `im:history`

如果你希望 bot 能读取更多类型的消息，可以按需再加:

- `channels:history`
- `groups:history`

当前这个项目的最小可用集合仍然是:

- `chat:write`
- `im:history`

如果你的目标只是“通过手机私聊控制 Codex”，那么:

- `chat:write`
- `im:history`

就已经够用，`app_mentions:read` 不是必须。

5. 开启事件订阅

- 进入左侧 `Features` -> `Event Subscriptions`
- 打开 `Enable Events`
- 因为本项目走 `Socket Mode`，这里不需要配置公网 Request URL
- 在 `Subscribe to bot events` 下添加:

- `app_mention`
- `message.im`

如果你后续希望 bot 在频道普通消息里也响应，而不是只响应 `@机器人` 或私聊，再按需增加别的 message 事件并改代码。

6. 安装应用到 workspace

- 回到 `OAuth & Permissions`
- 点击 `Install to Workspace`
- Slack 会弹出授权页面
- 确认安装
- 安装完成后你会得到一个 `xoxb-...` Bot User OAuth Token
- 把它填进 `.env` 里的 `SLACK_BOT_TOKEN`

7. 获取 Signing Secret

- 进入左侧 `Basic Information`
- 在 `App Credentials` 区域找到 `Signing Secret`
- 复制后填进 `.env` 里的 `SLACK_SIGNING_SECRET`

虽然当前 Socket Mode 下通常不依赖公网签名校验链路，但这个项目仍然读取这个配置，建议一并填上。

8. 检查 Bot 用户设置

- 进入左侧 `Features` -> `App Home`
- 确保允许 bot 以应用身份接收私聊
- 如果你希望直接私聊 bot，确认 `Messages Tab` 已开启

9. 重新安装应用

- 只要你改过 scopes 或事件订阅，都回到 `OAuth & Permissions`
- 再点一次 `Reinstall to Workspace`
- 否则新权限可能不会生效

10. 填写本地 `.env`

至少确认这些值已经正确:

```env
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...
ALLOWED_SLACK_USER_IDS=U0123456789
```

11. 启动并验证

- 本地启动:

```bash
python3 server.py
```

- 看到终端输出 `Bolt app is running!` 说明 Socket Mode 已连接成功
- 在 Slack 里验证两种场景:

1. 在一个频道里 `@你的机器人 说一句话`
2. 直接给机器人发私聊

12. 最小排障

- 能启动但频道里 `@机器人` 没反应:
  通常是缺少 `app_mentions:read`，或者改了权限后没有重新安装应用
- 私聊没反应:
  通常是缺少 `im:history`，或者 `App Home` / `Messages Tab` 没开
- 启动时报 token 相关错误:
  优先检查 `.env` 里是不是把 `xapp-...` 和 `xoxb-...` 填反了
- 改完 Slack 配置后仍然没效果:
  先 `Reinstall to Workspace`，再重启 `server.py`

## 运行方式

这个版本默认走 Socket Mode，所以:

- 不需要公网回调地址
- 不需要自己暴露 `/slack/events`
- 更适合直接跑在你的云服务器上

## 部署方式

```bash
python3 server.py
```

后续可以把它包装成 systemd 服务，开机自启。

## 代码说明

- `server.py`: Slack Bolt 应用、Socket Mode 入口、`pexpect` 调用 Codex
- `requirements.txt`: Python 依赖
- `.env.example`: 环境变量模板
- `.codex-slack-sessions.json`: Slack thread 到 Codex session 的本地缓存

## 会话复用

当前实现是“按 Slack thread 划分 Codex session”。

- session 的索引 key 是 `channel + ":" + thread_ts`
- 也就是说，同一个频道里的两个不同 thread，会对应两个不同的 Codex session
- 频道消息里如果你 `@机器人` 开了一个新 thread，这个 thread 的第一条任务会创建一个新的 Codex session
- 之后这个 thread 里的后续回复，会继续 `resume` 这个 thread 自己的 Codex session
- 不同 thread 之间不会共享上下文、不会共享 session id、不会互相污染历史
- 私聊场景里，每一条独立私聊消息如果没有复用同一个 `thread_ts`，也会被当成不同 thread；如果是在同一个私聊 thread 下继续回复，则会复用同一个 session
- 服务会把 `thread_key -> {session_id, owner_user_id}` 记录到本地的 `.codex-slack-sessions.json`
- 这个文件是本地缓存，所以 `server.py` 重启后，已有 thread 仍然可以继续找到对应的 session id
- 同一个 thread 内部会串行处理消息，避免并发 `resume` 导致上下文顺序错乱
- 如果同一个 Codex session 被绑定到多个 Slack thread，服务也会按 `session_id` 串行执行，避免不同 thread 并发 `resume` 同一个 session
- 在多用户白名单模式下，一旦某个 thread 或 session 已经归属于某个 Slack 用户，其他白名单用户不能继续使用它
- 如果某个 thread 的旧 session 恢复失败，服务会只丢弃这个 thread 的 session，并自动为这个 thread 重建新会话

当前 thread 级命令的作用域也是“只影响当前 Slack thread”。

- `/reset`：清掉当前 thread 的 Codex session，下条消息会新建 session
- `/fresh 你的任务`：忽略当前 thread 旧 session，这条消息强制新建一个 session，并把它设为当前 thread 的新 session
- `/session`：返回当前 thread 正在使用的 Codex session id
- `/attach <session_id>`：把当前 Slack thread 绑定到一个已有的 Codex session；默认只允许单用户白名单 attach 未见过的 session，多用户共享 attach 需要显式开启 `ALLOW_SHARED_ATTACH=1`
- `/where` / `/whoami` / `/status`：返回当前 thread 绑定的 `session_id`、`workdir`、模型等运行状态
- `/handoff`：基于当前 session 的已有上下文，生成一份适合跨端接力的短版 handoff note，并附带终端核验命令
- `/recap`：基于当前 session 的已有上下文，生成一份简短的最近进展总结
- `reset`：和 `/reset` 等价，适合手机里直接发普通文本
- `fresh 你的任务`：和 `/fresh 你的任务` 等价，适合手机里直接发普通文本
- `session`：和 `/session` 等价，适合手机里直接发普通文本
- `attach <session_id>`：和 `/attach <session_id>` 等价，适合手机里直接发普通文本
- `where` / `whoami` / `status`：和带斜杠版本等价，适合手机里直接发普通文本
- `handoff`：和 `/handoff` 等价，适合手机里直接发普通文本
- `recap`：和 `/recap` 等价，适合手机里直接发普通文本

手机里最稳的用法:

- 私聊 bot 时，直接发送普通文本，不要在私聊里再写 `@bot`
- 例如直接发 `session`
- 如果你想快速确认当前 thread 到底绑定了哪个会话、跑在哪个目录，直接发 `where`
- 如果你想接管一个终端里已有的 Codex 会话，先拿到它的 UUID 形式 `session id`，再在 Slack 里发 `attach <session_id>`
- 默认更安全的策略是：只有单用户白名单模式才允许直接 attach 一个“尚未被 bot 见过”的终端 session；多用户共享 attach 需要显式打开 `ALLOW_SHARED_ATTACH=1`
- 如果你准备把控制权从终端切到手机，或者从手机切回终端，先发 `handoff` 生成一份短版交接说明会更稳
- 如果你只是想在手机上快速回顾当前进展，不需要完整交接说明，直接发 `recap`
- 例如直接发 `fresh 到你的目标项目目录里总结一下当前状态`

可以用下面的例子理解：

- 频道 `#dev` 里 thread A 首次对机器人说话，会创建 session A
- 继续在 thread A 里回复，会继续使用 session A
- 同时在 `#dev` 再开 thread B 对机器人说话，会创建 session B
- thread A 和 thread B 互不影响

## 后续建议

现在这个版本是 MVP。下一步通常会补:

- 并发队列和任务取消
- 失败重试和更清晰的状态消息
- 多轮上下文存储
- 指定群聊白名单 / 用户白名单
- 额外命令路由，例如 `/model`
- 代码块格式化输出
