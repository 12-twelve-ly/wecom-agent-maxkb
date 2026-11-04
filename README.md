# wecom-agent-maxkb

把**企业微信智能机器人**和 **MaxKB 应用**连接起来的轻量网关。

- 企微侧：WebSocket 长连接，**不需要公网 IP、域名或回调地址**
- MaxKB 侧：调用应用聊天 API，保留你在 MaxKB 里配置的知识库 / 工作流
- 支持按企微用户隔离多轮会话、语音/文字提问、答题模式、用量导出
- 支持 `share_token`（分享链接）和 `api_key`（应用 API Key）两种认证
- 当前版本：**一个企微机器人 ↔ 一个 MaxKB 应用**（多机器人并行在后续版本）

---

## 1. 它是怎么工作的

```text
企微用户
   │  文字 / 语音
   ▼
企业微信服务器
   │  WebSocket 长连接（Bot ID + Secret 认证）
   ▼
本程序（bot.py）
   │  HTTP + Bearer
   ▼
MaxKB 应用（知识库 / 工作流 Agent）
   │
   ▼
知识库检索 + 大模型
```

两种凭据分工：

| 凭据 | 用途 |
|---|---|
| `WECOM_BOT_ID` + `WECOM_SECRET` | 认证企微智能机器人，建立长连接 |
| `MAXKB_ACCESS_TOKEN` 或 `MAXKB_API_KEY` | 认证 MaxKB 应用 |

---

## 2. 环境要求

| 项目 | 要求 |
|---|---|
| Python | 3.10+（3.11+ 更省事，内置 `tomllib`；3.10 会自动使用 `tomli`） |
| 企业微信 | 已创建「智能机器人」，并开启 API / 长连接模式 |
| MaxKB | 建议 v2；应用已发布，可被本机访问 |
| 操作系统 | Windows 10/11 或 Linux |
| 可选 | Docker（仅 `usage_report.py` 导出用量时需要，且需与 MaxKB 同机） |

网络要求：本程序能访问 `wss://openws.work.weixin.qq.com` 和你的 MaxKB 地址。

---

## 3. 获取凭据

### 3.1 企微机器人 Bot ID / Secret

1. 登录[企业微信管理后台](https://work.weixin.qq.com/)。
2. 进入「应用管理 → 智能机器人」，创建或选择机器人。
3. 打开开发者 / API 配置，复制 **Bot ID** 和 **Secret**。
4. 本程序使用 WebSocket 长连接，**不需要填写回调 URL**。

> 后台菜单名称可能随企微版本调整，以你看到的「智能机器人 → 开发配置」为准。

### 3.2 MaxKB 分享 token（方式一，默认）

1. 打开 MaxKB 应用 → 概览 / 公开访问设置。
2. 复制访问链接里的 `access_token`，形如：

```text
http://你的maxkb/chat/xxxx?access_token=xxxxxxxx
```

3. 将 `xxxxxxxx` 填入 `.env` 的 `MAXKB_ACCESS_TOKEN`。

应用需要允许公开访问 / 分享，否则 `/chat/api/auth/anonymous` 会认证失败。

### 3.3 MaxKB 应用 API Key（方式二）

1. 打开 MaxKB 应用 → API 访问 / API Key。
2. 创建 Key，复制以 `application-` 或 `agent-` 开头的字符串。
3. 在 `config.toml` 中设置：

```toml
[bot.maxkb]
auth_mode = "api_key"
api_key = "${MAXKB_API_KEY}"
# 可选：填写后 usage_report.py 无需反查
application_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

API Key 直接作为 Bearer 使用，多轮会话机制与分享 token 相同。

---

## 4. 快速开始（可复现）

### 4.1 克隆与安装

```bash
git clone https://github.com/12-twelve-ly/wecom-agent-maxkb.git
cd wecom-agent-maxkb

python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 4.2 填写密钥

```bash
# Windows: copy .env.example .env
cp .env.example .env
```

编辑 `.env`：

```dotenv
WECOM_BOT_ID=你的BotID
WECOM_SECRET=你的Secret
MAXKB_API_BASE=http://localhost:8081
MAXKB_ACCESS_TOKEN=你的分享token
```

> 使用 API Key 时，改为填 `MAXKB_AUTH_MODE=api_key` 和 `MAXKB_API_KEY=...`。

### 4.3 填写配置

```bash
# Windows: copy config.example.toml config.toml
cp config.example.toml config.toml
```

最小修改：

```toml
[[bot]]
name = "mybot"
enabled = true
wecom_bot_id = "${WECOM_BOT_ID}"
wecom_secret = "${WECOM_SECRET}"

[bot.maxkb]
base_url = "http://localhost:8081"
auth_mode = "share_token"
access_token = "${MAXKB_ACCESS_TOKEN}"

[bot.persona]
display_name = "我的知识助手"
```

`${VAR}` 会从 `.env` 或系统环境变量展开。

> 不创建 `config.toml` 也可以运行：会进入旧版 `.env` 单机器人模式，沿用 Doris 文案和 `conversations.json`。

### 4.4 自检

```bash
python bot.py --check
# 或指定配置
python bot.py --check --config config.toml
```

预期输出：

```text
配置正常: bot=mybot maxkb=http://localhost:8081 auth_mode=share_token
```

`--check` 会真实认证 MaxKB 并创建一个空会话（不发送问题）。

### 4.5 启动

```bash
python bot.py
```

看到日志 `已连接企微 bot=mybot，等待消息中...` 即启动成功。

Windows 也可以双击 `启动.bat`；存在 `config.toml` 时会自动传入 `--config`。

### 4.6 验证

在企微里给机器人发一条消息，应看到：

1. 先出现「思考中...」
2. 回答按流式逐步刷新
3. 发送「菜单」能收到配置中的菜单文案

---

## 5. 配置说明

```toml
[server]
state_dir = "data"          # 会话文件目录
log_level = "INFO"          # DEBUG / INFO / WARNING / ERROR / CRITICAL

[[bot]]
name = "mybot"              # 小写字母、数字、_、-，用于状态文件和日志
enabled = true
wecom_bot_id = "${WECOM_BOT_ID}"
wecom_secret = "${WECOM_SECRET}"

[bot.maxkb]
base_url = "http://localhost:8081"
auth_mode = "share_token"   # share_token | api_key
access_token = "${MAXKB_ACCESS_TOKEN}"
# api_key = "${MAXKB_API_KEY}"
# application_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

[bot.persona]
display_name = "我的知识助手"
welcome = "你好，我是 {name}，直接发问题就行。发送「重置」可清除上下文重新开始。"
menu = "**{name}使用说明**\n> ..."
reset = "**{name}** · 上下文已重置\n> **上下文：** 0 / {max_context} 轮"
max_context = 10
```

文案占位符：

| 占位符 | 替换为 |
|---|---|
| `{name}` | `persona.display_name` |
| `{max_context}` | `persona.max_context` |

当前版本要求 **有且只有一个 `enabled = true` 的 bot**；配置多个会提示“多机器人将在后续版本支持”。

---

## 6. 更换机器人 / 更换 Agent

改完配置后**重启进程**生效。

| 想换什么 | 修改 |
|---|---|
| 企微机器人 | `wecom_bot_id` / `wecom_secret` |
| MaxKB 实例 | `bot.maxkb.base_url` |
| MaxKB 应用（分享 token） | `access_token` |
| MaxKB 应用（API Key） | `auth_mode = "api_key"` + `api_key` |
| 展示名 / 菜单 / 欢迎语 | `[bot.persona]` |

```bash
# Linux / systemd
systemctl restart wecom-coze-bot
# Windows
# 关闭窗口后重新运行 启动.bat
```

> 同一个企微 Bot ID 同时只允许一个有效长连接。切换时请确保旧进程已退出。

---

## 7. 会话与数据

- 每个企微 `userid` 对应一个 MaxKB `chat_id`，保存在 `data/{bot.name}.conversations.json`。
- 第一次使用 `config.toml` 启动时，如果 `data/` 下没有会话文件，会自动把根目录旧的 `conversations.json` 复制过去（原文件保留）。
- 用户发「重置」只清本地映射，不删除 MaxKB 服务端聊天记录。
- 会话失效（token 过期 / 对话不存在）会自动重建并重试一次。

---

## 8. 企微内可用指令

| 指令 | 作用 |
|---|---|
| 直接发文字 / 语音 | 向 MaxKB 提问 |
| `菜单` / `帮助` / `help` | 显示菜单 |
| `重置` / `/reset` | 清除上下文，新开会话 |
| `答题` | 进入答题模式 |
| `回答完毕` / `提交回答` | 一次性提交答题缓冲 |
| `取消答题` | 放弃本次答题 |

答题模式下，多段语音/文字会先拼接，静默 4 分钟自动提交。

---

## 9. 用量统计与问答明细

`usage_report.py` 通过 `docker exec maxkb psql` 只读查询 MaxKB 数据库，需要**在与 MaxKB 同一台机器**上运行，或能访问该 Docker 容器。

```bash
# 最近 7 天汇总
python usage_report.py --days 7

# 每条问答明细
python usage_report.py --detail --days 7

# 全部历史导出 CSV（含时间、用户、问答、token）
python usage_report.py --detail --all --out usage.csv

# 导出原始估算值（默认 ×0.3 校准）
python usage_report.py --detail --all --raw --out usage_raw.csv
```

明细列：

```text
时间, 用户, 提问内容, 返回内容, 输入token, 输出token, 总消耗token
```

说明：

- 默认按配置/`.env` 里的 MaxKB 应用过滤，避免统计到同一实例的其他应用。
- `--all-apps` 可统计整个 MaxKB 实例（可能包含其他应用，慎用）。
- 容器名 / DB 账号可通过环境变量覆盖：`MAXKB_DB_CONTAINER`、`MAXKB_DB_USER`、`MAXKB_DB_NAME`。

---

## 10. Linux systemd 部署（可选）

项目自带 `wecom-coze-bot.service`，按实际路径修改后：

```ini
[Unit]
Description=WeCom Smart Bot <-> MaxKB Relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/wecom-agent-maxkb
ExecStart=/home/ubuntu/wecom-agent-maxkb/.venv/bin/python /home/ubuntu/wecom-agent-maxkb/bot.py --config /home/ubuntu/wecom-agent-maxkb/config.toml
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wecom-coze-bot
journalctl -u wecom-coze-bot -f
```

---

## 11. 常见问题

| 现象 | 原因与处理 |
|---|---|
| `配置错误：缺少配置项 ...` | `.env` 或 `config.toml` 缺少对应字段 |
| `MaxKB 认证失败: HTTP 401/403` | 分享 token 失效、应用未公开，或 API Key 无效 |
| `MaxKB API Key 无效或已过期` | 到 MaxKB 重新创建 API Key 并更新配置 |
| 用户“失忆” | MaxKB 会话/token 失效，程序已自动新建会话；分享 token 过期会导致上下文丢失 |
| 回答被截断 | 企微单条消息上限 20480 字节，程序会截断并提示 |
| 机器人不回消息 | 检查 `Bot ID/Secret`、网络、日志；确认同一 Bot ID 没有另一个实例在线 |
| `MaxKB 请求超时（300 秒）` | MaxKB 或模型响应过慢，检查 MaxKB 日志/模型服务 |
| `usage_report.py` 没有数据 | MaxKB 不在本机、容器名不是 `maxkb`，或当前应用过滤不匹配；可试 `--all-apps` |
| 修改配置没生效 | 需要重启进程 |

查看日志：

```bash
# Linux
journalctl -u wecom-coze-bot -f
# Windows
# 直接看 启动.bat 的窗口输出
```

---

## 12. 安全

- `.env`、`config.toml`、`data/`、导出的 `usage*.csv` 不要提交到 git（`.gitignore` 已覆盖）。
- Linux：启动时会尝试把 `.env`、配置文件、状态文件设为 0600，状态目录 0700。
- Windows：`conversations.json` 每次保存都会重建，必须从**目录层面**收紧 ACL：

```powershell
$dir = "E:\path\to\wecom-agent-maxkb"
icacls $dir /inheritance:r
icacls $dir /grant:r "$env:USERNAME:(OI)(CI)M" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"
icacls $dir /remove:g "Authenticated Users" "BUILTIN\Users"

icacls "$dir\.env" /inheritance:r
icacls "$dir\.env" /grant:r "$env:USERNAME:R" "SYSTEM:F" "Administrators:F"
```

- 迁移到 `data/` 后，根目录旧的 `conversations.json` 仍含有效会话 token；确认不再回退旧模式后请删除。
- 凭据泄露后请轮换 `WECOM_SECRET`、`MAXKB_ACCESS_TOKEN` / API Key。

---

## 13. 开发与测试

```bash
python -m unittest discover -s tests -v
```

自检 MaxKB 连通性：

```bash
python bot.py --check --config config.example.toml
```

注意：`--check` 会使用真实凭据访问 MaxKB 并创建一个空会话。

项目结构：

```text
bot.py              入口：配置加载、状态迁移、企微消息处理、--check
config.py           config.toml/.env 解析、校验、persona 文案
maxkb_client.py     MaxKB 认证、SSE、重试、会话重建
usage_report.py     用量汇总与问答明细导出
config.example.toml 配置样例
.env.example        密钥样例
tests/              配置、MaxKB 客户端、运行时、报表测试
```

---

## 14. 版本与路线

- 当前：单企微机器人 ↔ 单 MaxKB 应用，配置化切换 Bot/Agent。
- 后续：一个进程并行多个机器人，每个绑定不同 MaxKB 应用；热重载、实时用量日志等。
