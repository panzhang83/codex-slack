# codex-slack

一个面向本机 Codex CLI 的最小 Slack 桥接服务：

- Slack 通过 Socket Mode 把消息推给本机 `server.py`
- `server.py` 会通过官方 `codex-app-server-sdk` 运行由 Slack 接管的 turn，并用官方 thread 视图做 `watch`
- `watch` 会在对话发生变化时把当前 Codex session 的 thread 对话同步到 Slack，方便在手机端旁路观察

## 当前能力

- 频道里响应 `@bot`
- 私聊里直接响应文本消息
- 每个 Slack thread 绑定一个 Codex session
- 同一个 Slack thread 会继续复用同一个 session
- `attach <session_id>` 可把当前 Slack thread 绑定到一个已有 session
- `recent` / `sessions` 可查看最近的 Codex sessions，并支持 `attach recent <n>`
- `attach` 后默认进入 `observe` 模式，避免和终端里的交互式 Codex 并发写入
- 只有切到 `control` / `takeover` 模式后，Slack 普通消息才会继续 `resume` 当前 session
- 支持把 Slack 消息里的图片附件和文档类附件传给 Codex
- `watch` 会先回放最近一轮已完成的可显示对话，然后在 thread 对话发生变化时持续推送后续新增的用户消息和 `final_answer`
- `name <title>` 可重命名当前 session
- `interrupt` / `steer <text>` 可控制当前由 `codex-slack` 接管并持有的活跃 turn
- 默认会推送自然语言中间 `Codex Progress`，并做短时间节流合并；可按 Slack thread 用 `progress on|off|reset|status` 控制
- 支持按 Slack thread 设置 reasoning effort：`effort <level>`、`effort reset`、`fresh --effort <level> ...`
- App Home 会显示默认配置、你自己的 Slack thread 绑定和最近 sessions
- App Home 里的 binding 行支持 `Rename`，可直接改当前绑定 session 的标题
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
CODEX_REASONING_EFFORT=xhigh
CODEX_SANDBOX=danger-full-access
CODEX_FULL_AUTO=0
CODEX_EXTRA_ARGS=
CODEX_SLACK_SESSION_STORE=/path/to/codex-slack/.codex-slack-sessions.json
CODEX_SLACK_WATCH_POLL_SECONDS=5
CODEX_PROGRESS_UPDATES=1
CODEX_PROGRESS_HEARTBEAT_SECONDS=300
CODEX_PROGRESS_POLL_SECONDS=15
CODEX_PROGRESS_BATCH_SECONDS=5
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
- `CODEX_REASONING_EFFORT` 控制由 Slack 新建或自动重建的 session 默认 effort；只支持 `low|medium|high|xhigh`
- 如果没有设置 `CODEX_REASONING_EFFORT`，Slack 新建或自动重建的 session 会默认使用 `xhigh`
- `CODEX_WORKDIR` 控制由 Slack 新建或自动重建的 session 默认工作目录
- 如果当前 Slack thread 是通过 `attach <session_id>` 绑定到一个已有 session，并且服务成功识别出该 session 的原始工作目录，那么后续 `control` / `takeover` 时会继续沿用那个目录，而不是强制切回 `CODEX_WORKDIR`
- `attach` 一个已有 session 时，不会自动改动那个 session 原本的 effort；只有显式执行 `effort ...` 或 `fresh --effort ...` 才会覆盖
- 当前不支持 `auto`、`none`、`minimal` 之类的 effort 值
- `CODEX_SLACK_WATCH_POLL_SECONDS` 控制持续 watch 的轮询间隔，默认 5 秒
- `CODEX_PROGRESS_UPDATES=1` 表示默认开启中间 `Codex Progress` 推送；设为 `0` 则默认关闭
- `CODEX_PROGRESS_HEARTBEAT_SECONDS` 控制长任务 heartbeat 间隔，默认 300 秒
- `CODEX_PROGRESS_POLL_SECONDS` 控制长任务 progress 轮询间隔，默认 15 秒
- `CODEX_PROGRESS_BATCH_SECONDS` 控制 progress 在发往 Slack 前的短时间合并窗口，默认 5 秒
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
- `files:read`
- `app_mentions:read`

如果你只打算私聊控制，`chat:write` 和 `im:history` 是最低必需项；如果要把 Slack 附件传给 Codex，则还需要 `files:read`；如果要在频道里 `@bot`，则还需要 `app_mentions:read`。

4. 配置 Event Subscriptions

- 打开 `Event Subscriptions`
- 因为这里走 Socket Mode，不需要公网 Request URL
- 在 `Subscribe to bot events` 添加：

- `app_mention`
- `message.im`
- `app_home_opened`

5. 安装或重装到 workspace

- 到 `OAuth & Permissions`
- 点击 `Install to Workspace` 或 `Reinstall to Workspace`
- 把拿到的 `xoxb-...` 填入 `.env` 的 `SLACK_BOT_TOKEN`

6. 获取 Signing Secret

- `Basic Information` -> `App Credentials`
- 复制 `Signing Secret`
- 填入 `.env` 的 `SLACK_SIGNING_SECRET`

7. 确认私聊入口

- `App Home` 里启用 `Home Tab`
- `App Home` 里启用 `Messages Tab`

8. 开启交互回调

- 打开 `Interactivity & Shortcuts`
- 打开 `Interactivity`
- 因为这里走 Socket Mode，不需要公网 Request URL

## 常用命令

这些命令既支持斜杠形式，也支持普通文本形式：

- `reset` / `/reset`：清掉当前 Slack thread 的 session
- `fresh <prompt>` / `/fresh <prompt>`：忽略旧 session，强制新建会话
- `fresh --effort <level> <prompt>`：先把当前 Slack thread 的 effort override 设为 `<level>`，再强制新建会话
- `session` / `/session`：查看当前 Slack thread 绑定的 session id
- `attach <session_id>` / `/attach <session_id>`：把当前 Slack thread 绑定到已有 session，默认进入 `observe`
- `recent` / `/recent`：按当前生效工作目录列出最近的 Codex sessions
- `sessions` / `/sessions`：列出当前范围下最近的 Codex sessions
- `sessions --all`：列出全局最近的 Codex sessions
- `sessions --cwd <path>`：列出指定工作目录下最近的 Codex sessions
- `attach recent <n>`：把当前 Slack thread 绑定到最近一次 `recent` 或 `sessions` 列表里的第 `n` 个 session
- `name <title>`：重命名当前 Slack thread 绑定的 session
- `effort`：查看当前 Slack thread 的 reasoning effort 状态
- `effort <low|medium|high|xhigh>`：设置当前 Slack thread 后续由 Slack 发起 turns 的 effort
- `effort reset`：清除当前 Slack thread 的 effort override
- `where` / `whoami` / `status`：查看当前 thread 的绑定状态
- `watch`：显示最近一轮对话，并持续推送后续新增对话
- `unwatch` / `stop watch`：停止持续 watch
- `control` / `takeover`：切到 `control` 模式，允许 Slack 普通消息继续 `resume`；如果当前 thread 上有 `watch`，会自动停止以避免重复消息
- `observe` / `release`：切回 `observe` 模式
- `progress` / `progress status`：查看当前 Slack thread 的中间 progress 推送状态
- `progress on` / `progress off`：开启或关闭当前 Slack thread 的中间 progress 推送
- `progress reset`：清除当前 Slack thread 的 progress 覆盖，回退到 `.env` 默认值
- `interrupt`：向当前 session 的活跃 turn 发送中断请求
- `steer <text>`：向当前 session 的活跃 turn 追加一条指令；只在 `control` 模式下可用
- `handoff`：基于当前 session 生成一份简短交接说明
- `recap`：基于当前 session 生成一份简短进展总结

## Reasoning Effort

支持的 effort 值只有这四个：

- `low`
- `medium`
- `high`
- `xhigh`

行为说明：

- `effort <level>` 会给“当前 Slack thread”设置一个 override；后续由这个 Slack thread 发起的 turn 都会使用该值
- `effort reset` 会清掉这个 override
- 如果当前 thread 是通过 `attach <session_id>` 绑定到一个已有 session，并且没有设置 override，那么 Slack 后续 `resume` 会继续继承原 session 里的 effort，不会强行改成 `xhigh`
- 如果当前 thread 是由 Slack 自己新建或自动重建出来的 session，则会使用 `CODEX_REASONING_EFFORT`；如果 `.env` 里没配，默认是 `xhigh`
- `fresh --effort high <prompt>` 等价于“先设置当前 thread 的 override，再立即用这个 effort 新开一次会话”
- `status` / `where` / `whoami` 会显示当前 thread 的 effort 状态，包括 thread override、`.env` 默认值，以及当前生效来源

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
- 如果你刚发过 `recent` 或 `sessions`，也可以直接用 `attach recent <n>`
- 绑定后默认是 `observe` 模式，适合“终端主控，手机旁路观察”
- 如果服务识别到了该 session 的工作目录，那么之后 Slack `control` / `takeover` 时会继续沿用这个目录
- `watch` 只显示 thread 对话里的用户消息和 agent `final_answer`
- 对于只是 `attach` 进来的终端活跃 turn，`watch` 是旁路镜像，不会直接改写终端那一轮
- 当前 Slack thread 处于 `control` 模式时，不再启动 `watch`；因为后续回复本来就会直接发到这个 thread，继续镜像只会造成重复消息
- `watch` 首次会回放最近一轮已完成的可显示对话；如果当前最新 turn 还没出 `final_answer`，会等后续增量推送
- `watch` 会尽量只在 thread 实际发生变化时刷新镜像，而不是持续重发整段历史
- 后续只推送新出现的用户消息和 agent `final_answer`
- 对于 `control` 模式下的长任务，服务会在运行期间定时发送 heartbeat；默认也会推送自然语言中间 `Codex Progress`
- 多条相邻的 progress 会在短时间窗口内自动合并，减少 Slack 消息噪音和明显卡顿
- 这些 progress 默认开启；如果你只想看最终结果，可以在当前 Slack thread 里发送 `progress off`
- 之后如果想恢复默认行为，发送 `progress on`；如果想回退到 `.env` 里的默认值，发送 `progress reset`
- 为了和普通说明消息区分，镜像对话会用 `*User*` / `*Codex*` 标题加引用块样式发送到 Slack
- 如果 `server.py` 重启了，session 绑定仍然保留，但正在运行的 `watch` 不会自动恢复；需要你在同一个 Slack thread 里重新发一次 `watch`
- 如果 `watch` 因为读取失败而停止，直接重新发送一次 `watch` 即可重建镜像
- 如果你不想再持续推送，发 `unwatch` 或 `stop watch`
- 如果你想改为由 Slack 接管，再发 `control` 或 `takeover`；这时当前 thread 上已有的 `watch` 会自动停止

## `recent`、`sessions` 和 App Home

- `recent` 默认只看“当前生效工作目录”下最近的 Codex sessions
- `sessions` 默认和 `recent` 类似，但支持显式范围控制
- `sessions --all` 会忽略工作目录过滤，显示全局最近 sessions
- `sessions --cwd /path/to/project` 会按你给定的目录过滤
- 列表里的序号只在当前 Slack thread 内暂存一段时间，所以如果你想用 `attach recent <n>`，最好紧接着在同一个 Slack thread 里发送
- App Home 是一个操作面板，方便你快速看到默认 model / effort / workdir、你自己的 Slack thread 绑定，以及最近 sessions
- App Home 里的 binding 会优先显示 session 当前标题；如果还没有显式标题，则退回显示 Slack thread 类型
- 你可以直接在 App Home 点击 `Rename`，用 Slack modal 给这个绑定的 session 改名
- App Home 不会替代 Slack thread 控制；真正的 `attach`、`watch`、`takeover`、`steer` 仍然在消息 thread 里完成

## `interrupt` 和 `steer`

- `interrupt` / `steer` 作用于当前由 `codex-slack` runtime 持有的活跃 turn
- 如果当前只是 `attach` 到一个终端里正在运行的 turn，那么它仍然是 `watch`-only；Slack 不会直接打断或 steer 那一轮
- `interrupt` 在 `observe` 和 `control` 模式下都可以使用，但前提是当前活跃 turn 已经是由 Slack 这边接管后启动的
- `steer <text>` 会向当前活跃 turn 追加一条指令
- `steer` 只在 `control` 模式下可用，避免你在只读镜像模式里意外写入
- 如果 `server.py` 在某个活跃 turn 运行期间重启，那么该 turn 的 session 绑定仍保留，但 runtime 持有状态不会自动恢复；这时你仍可 `watch`，但对这一个已经在运行的 turn 不能继续 `interrupt` 或 `steer`
- 等那一轮结束后，在 Slack 里继续发送普通消息启动由 `codex-slack` 接管的新 turn，之后 `interrupt` / `steer` 会再次可用
- 这两个命令都依赖当前 session 里确实存在活跃 turn；如果当前没有正在运行的 turn，Slack 会直接返回错误说明

## 附件输入

- 如果你在私聊或 `@bot` thread 里发送图片附件，服务会把图片下载到本地临时目录，并通过 Codex CLI 的 `--image` 传给模型
- 如果你发送文档类附件，服务会把文件下载到本地临时目录，并把本地文件清单附加到 prompt，让 Codex 直接读取这些路径
- 文档类附件当前支持常见文本/源码/配置文件，以及 `pdf`、`docx`、`jl`、`ipynb`
- 如果消息里既有文字也有附件，文字会作为正常 prompt，附件会作为附加输入
- 如果你只发附件不写文字，服务会自动补一条默认提示，让 Codex 先基于这些附件继续处理
- 附件下载完成后会在本轮结束后自动清理临时文件
- 当前不处理压缩包和其他未列出的二进制附件
- 这项能力依赖 Slack scope `files:read`

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
python3 -m py_compile server.py app_runtime.py codex_threads.py session_catalog.py turn_control.py slack_home.py slack_image_inputs.py slack_document_inputs.py tests/test_server.py tests/test_session_catalog.py tests/test_turn_control.py tests/test_slack_home.py tests/test_slack_image_inputs.py tests/test_slack_document_inputs.py
python3 -m unittest -q tests.test_server tests.test_session_catalog tests.test_turn_control tests.test_slack_home tests.test_slack_image_inputs tests.test_slack_document_inputs
```

## 文件说明

- `server.py`：Slack Socket Mode 服务和 Codex session 管理
- `app_runtime.py`：长生命周期 app-server runtime，负责 Slack 接管后的 turn 执行和 live steer / interrupt
- `codex_threads.py`：官方 app-server thread 读写封装
- `session_catalog.py`：recent / sessions 列表和 `attach recent <n>` 选择缓存
- `turn_control.py`：活跃 turn 状态辅助和本地 registry
- `slack_home.py`：App Home 仪表板视图
- `slack_image_inputs.py`：Slack 图片附件提取、下载和清理
- `slack_document_inputs.py`：Slack 文档类附件提取、下载和 prompt 注入
- `tests/test_server.py`：主流程和命令路由测试
- `tests/test_session_catalog.py`：recent / sessions 列表测试
- `tests/test_turn_control.py`：turn 控制测试
- `tests/test_slack_home.py`：App Home 视图测试
- `tests/test_slack_image_inputs.py`：Slack 图片输入测试
- `tests/test_slack_document_inputs.py`：Slack 文档输入测试
- `.env.example`：环境变量模板
- `requirements.txt`：Python 依赖
