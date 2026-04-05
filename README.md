# codex-slack

一个面向本机 Codex CLI 的最小 Slack 桥接服务：

- Slack 通过 Socket Mode 把消息推给本机 `server.py`
- `server.py` 用本机 `codex exec --json` 流式调用 Codex CLI
- `watch` 会把当前 Codex session 的 thread 对话同步到 Slack，方便在手机端旁路观察

## 当前能力

- 频道里响应 `@bot`
- 私聊里直接响应文本消息
- 每个 Slack thread 绑定一个 Codex session
- 同一个 Slack thread 会继续复用同一个 session
- `attach <session_id>` 可把当前 Slack thread 绑定到一个已有 session
- `attach` 后默认进入 `observe` 模式，避免和终端里的交互式 Codex 并发写入
- 只有切到 `control` / `takeover` 模式后，Slack 普通消息才会继续 `resume` 当前 session
- `watch` 会先回放最近一轮已完成的可显示对话，然后持续推送后续新增的用户消息和 `final_answer`
- 支持白名单 `ALLOWED_SLACK_USER_IDS`
- 同一个 session 会按 `session_id` 串行执行，避免多个 Slack thread 并发 `resume`

## 安装

1. 复制环境变量模板

```bash
cp .env.example .env
```

2. 安装依赖

```bash
pip install -r requirements.txt
```

需要使用 Python `3.12+`，因为 `codex-app-server-sdk>=0.3` 官方包当前从 Python 3.12 开始提供。

当前依赖里包含：

- `slack-bolt`
- `codex-app-server-sdk>=0.3`

3. 填写 `.env`

```env
OPENAI_MODEL=gpt-5.4

SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...
ALLOWED_SLACK_USER_IDS=U0123456789
ALLOW_SHARED_ATTACH=0

CODEX_BIN=codex
CODEX_WORKDIR=/path/to/workdir
CODEX_TIMEOUT_SECONDS=900
CODEX_SANDBOX=danger-full-access
CODEX_FULL_AUTO=0
CODEX_EXTRA_ARGS=
CODEX_SLACK_SESSION_STORE=/path/to/codex-slack/.codex-slack-sessions.json
CODEX_SLACK_WATCH_POLL_SECONDS=5
CODEX_PROGRESS_HEARTBEAT_SECONDS=300
CODEX_PROGRESS_POLL_SECONDS=15
# CODEX_SLACK_APP_SERVER_LINE_LIMIT_BYTES=33554432
```

说明：

- `ALLOWED_SLACK_USER_IDS` 留空表示不限制；如果填写，则只有这些 Slack `user_id` 可以使用 bot
- 多个 `user_id` 用英文逗号分隔
- `ALLOW_SHARED_ATTACH=0` 是更安全的默认值
- 单用户白名单模式下，允许直接 `attach` 一个尚未被 bot 见过的 session
- 多用户共享 `attach` 需要显式设置 `ALLOW_SHARED_ATTACH=1`
- 即使开启 `ALLOW_SHARED_ATTACH=1`，一旦某个 Slack thread 或 session 已经绑定给某个 Slack 用户，其他白名单用户也不能接管
- `CODEX_TIMEOUT_SECONDS=0` 表示不设执行超时上限；适合超长任务
- `CODEX_SLACK_WATCH_POLL_SECONDS` 控制持续 watch 的轮询间隔，默认 5 秒
- `CODEX_PROGRESS_HEARTBEAT_SECONDS` 控制长任务 heartbeat 间隔，默认 300 秒
- `CODEX_PROGRESS_POLL_SECONDS` 控制长任务 progress 轮询间隔，默认 15 秒
- `CODEX_SLACK_APP_SERVER_LINE_LIMIT_BYTES` 可选，用于在 thread 很长时提高 app-server `thread/read` 的 stdio 行缓冲上限
- 系统环境变量优先级高于 `.env`

4. 先确认本机 `codex` 已登录可用

```bash
codex exec --skip-git-repo-check "reply with exactly OK"
```

5. 启动服务

```bash
python3 server.py
```

只保留一个 `server.py` 进程运行。当前实现会持有 `.codex-slack.pid` 锁文件；如果已经有一个实例在跑，第二个实例会直接退出。

## Slack 配置

1. 创建 Slack App

- 打开 `https://api.slack.com/apps`
- `Create New App`
- `From scratch`

2. 开启 Socket Mode

- `Settings` -> `Socket Mode`
- 打开 `Enable Socket Mode`
- 创建一个 `xapp-...` token
- 给它加 `connections:write`
- 填入 `.env` 的 `SLACK_APP_TOKEN`

3. 配置 Bot Scopes

在 `OAuth & Permissions` 的 `Bot Token Scopes` 添加：

- `chat:write`
- `im:history`
- `app_mentions:read`

如果你只打算私聊控制，`chat:write` 和 `im:history` 是最低必需项；如果要在频道里 `@bot`，则还需要 `app_mentions:read`。

4. 配置 Event Subscriptions

- 打开 `Event Subscriptions`
- 因为这里走 Socket Mode，不需要公网 Request URL
- 在 `Subscribe to bot events` 添加：

- `app_mention`
- `message.im`

5. 安装或重装到 workspace

- 到 `OAuth & Permissions`
- 点击 `Install to Workspace` 或 `Reinstall to Workspace`
- 把拿到的 `xoxb-...` 填入 `.env` 的 `SLACK_BOT_TOKEN`

6. 获取 Signing Secret

- `Basic Information` -> `App Credentials`
- 复制 `Signing Secret`
- 填入 `.env` 的 `SLACK_SIGNING_SECRET`

7. 确认私聊入口

- `App Home` 里启用 `Messages Tab`

## 常用命令

这些命令既支持斜杠形式，也支持普通文本形式：

- `reset` / `/reset`：清掉当前 Slack thread 的 session
- `fresh <prompt>` / `/fresh <prompt>`：忽略旧 session，强制新建会话
- `session` / `/session`：查看当前 Slack thread 绑定的 session id
- `attach <session_id>` / `/attach <session_id>`：把当前 Slack thread 绑定到已有 session，默认进入 `observe`
- `where` / `whoami` / `status`：查看当前 thread 的绑定状态
- `watch`：显示最近一轮对话，并持续推送后续新增对话
- `unwatch` / `stop watch`：停止持续 watch
- `control` / `takeover`：切到 `control` 模式，允许 Slack 普通消息继续 `resume`；如果当前 thread 上有 `watch`，会自动停止以避免重复消息
- `observe` / `release`：切回 `observe` 模式
- `handoff`：基于当前 session 生成一份简短交接说明
- `recap`：基于当前 session 生成一份简短进展总结

## `attach` 和 `watch`

推荐流程：

1. 在终端里运行或恢复你的 Codex 会话
2. 在该 Codex 会话内部执行：

```bash
printenv CODEX_THREAD_ID && pwd
```

3. 在手机 Slack 里发：

```text
attach <session_id>
```

4. 然后发：

```text
watch
```

行为说明：

- `attach` 只负责把“当前 Slack thread”绑定到那个明确的 session
- 绑定后默认是 `observe` 模式，适合“终端主控，手机旁路观察”
- `watch` 只显示 thread 对话里的用户消息和 agent `final_answer`
- 当前 Slack thread 处于 `control` 模式时，不再启动 `watch`；因为后续回复本来就会直接发到这个 thread，继续镜像只会造成重复消息
- `watch` 首次会回放最近一轮已完成的可显示对话；如果当前最新 turn 还没出 `final_answer`，会等后续增量推送
- 后续只推送新出现的用户消息和 agent `final_answer`
- 对于 `control` 模式下的长任务，服务会在运行期间定时发送 heartbeat；一旦服务拿到当前 session id，就会基于官方 thread turns 推送一部分中间 progress 更新
- 为了和普通说明消息区分，镜像对话会用 `*User*` / `*Codex*` 标题加引用块样式发送到 Slack
- 如果 `server.py` 重启了，session 绑定仍然保留，但正在运行的 `watch` 不会自动恢复；需要你在同一个 Slack thread 里重新发一次 `watch`
- 如果 `watch` 因为读取失败或对话锚点失效而停止，直接重新发送一次 `watch` 即可重建镜像
- 如果你不想再持续推送，发 `unwatch` 或 `stop watch`
- 如果你想改为由 Slack 接管，再发 `control` 或 `takeover`；这时当前 thread 上已有的 `watch` 会自动停止

## 白名单和 User ID

`ALLOWED_SLACK_USER_IDS` 填的是 Slack 成员自己的 `user_id`，不是 bot 的 id。

获取方式：

- 在 Slack 桌面版或网页版打开你的个人资料
- 右上角 `...`
- 选择 `Copy member ID`

## Session 模型

当前实现是“按 Slack thread 绑定 session”：

- key 形如 `channel:thread_ts`
- 同一个 Slack thread 会复用同一个 session
- 不同 thread 之间不会共享上下文
- 绑定关系会持久化到本地 `.codex-slack-sessions.json`
- `server.py` 重启后，已有 thread 仍可继续找到对应 session
- `watch` 的后台推送线程不持久化；如果服务重启，需要在原 Slack thread 里重新发送 `watch`
- 同一个 Slack thread 内部会串行处理，避免并发 `resume`
- 如果多个 Slack thread 指向同一个 session，也会按 `session_id` 串行执行

## 开发

运行语法检查和测试：

```bash
python3 -m py_compile server.py tests/test_server.py
python3 -m unittest tests.test_server -q
```

## 文件说明

- `server.py`：Slack Socket Mode 服务和 Codex session 管理
- `tests/test_server.py`：当前核心测试
- `.env.example`：环境变量模板
- `requirements.txt`：Python 依赖
