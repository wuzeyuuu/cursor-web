# Cursor Web

[English](README.md) | [中文](README.zh.md)

**核心目的：用手机随时随地写代码。**  
Agent 跑在家里那台电脑上，改的是那台机器上的文件。你人在地铁里、床上、外面，浏览器打开就能下指令。

纯 Python 标准库，不用 pip。Python 3.10+。

## 为什么会有这个项目（对 Cursor 的吐槽）

Cursor 把「AI 写代码」卖得很大，真正能改你仓库的 Agent 却被焊死在桌面 IDE 里。离开那扇编辑器窗口，能力基本归零。

更气的是同一家产品、同一份账单，会话还是两套皮：

- IDE 侧边栏一份（`agent-transcripts` / composer）
- Agent CLI 另一份（`~/.cursor/chats`）

官方也承认两边 **不能** `--resume` 互通。你在电脑上聊到一半的上下文，出了门就接不上。Cloud / 手机同步走的是云端 Agent，不是你这台机子上的本地 CLI。想在手机上继续「改我硬盘里的代码」，官方没有这条路。

所以这个项目很简单：把 **本机 Cursor Agent CLI** 套一层能在手机上用的网页。不是再造一个 IDE，是把已经付过钱的本地 Agent 从显示器前面解放出来。

网页聊天 **不是** Cursor IDE 侧边栏。它是对着你设定的 workspace 跑的 CLI 会话。这套分裂是 Cursor 自己的，不是本仓库的 bug。

会话列表会 **只读展示** 同一 workspace 的 IDE 记录（`~/.cursor/projects/.../agent-transcripts`），方便在手机上翻电脑上聊过的内容。官方仍然不能 `--resume` IDE 的 id；在 IDE 会话里点发送，会新开一条 CLI 会话，不会把两边拼回去。

## 能做什么

- 带密码的网页（cookie 会话），手机能用
- 用 SSE 流式输出 `agent -p --output-format stream-json`
- 续上 **CLI** 会话（`~/.cursor/chats`）
- 只读展示同一仓库的 **IDE** 会话；发送会开新的 CLI 会话
- 模式：agent / ask / plan；模型 + 快速档
- 手机底部拉出会话列表；任务运行中发送键变成终止

## 目录

```
server.py              入口
app/
  config.py            路径、环境变量、agent 二进制发现
  store.py             state.json / secrets.json
  auth.py              密码和会话
  jobs.py              进行中的任务状态
  chats.py             读 CLI sqlite + IDE jsonl
  agent_cli.py         拉起 / 流式输出 / 模型列表
  http_handler.py      HTTP 路由
  main.py
templates/             登录页 + 聊天页
```

运行时文件（已 gitignore）：`secrets.json`、`state.json`、`.env`。

## 安装 Cursor Agent CLI

网页服务会调用本机的 `agent`，先装好它。

Windows（PowerShell）：

```powershell
irm 'https://cursor.com/install?win32=true' | iex
```

macOS / Linux / WSL：

```bash
curl https://cursor.com/install -fsS | bash
```

如果不在 PATH 上，设置 `AGENT_BIN`。原生 Windows 安装器一般会放到 `%LOCALAPPDATA%\cursor-agent\agent.cmd`。

API key 在 [Cursor Dashboard → API Keys](https://cursor.com/dashboard/api) 生成。粘到页面 **设置** 里，或设置 `CURSOR_API_KEY`。

## 运行

```bash
cd cursor_web
copy .env.example .env    # Windows 用 copy  |  Unix 用 cp .env.example .env
```

编辑 `.env`：

- `AGENT_WEB_PASSWORD` — 第一次运行需要（会哈希进 `secrets.json`，不明文保存）
- `AGENT_WORKSPACE` — Agent 可以改的目录（若本目录在 `third_party/cursor_web`，默认是上上级仓库）
- `CURSOR_API_KEY` — 也可在网页设置里粘贴
- `AGENT_BIN` — `agent` 已在 PATH 上则可省略
- `AGENT_WEB_HOST` / `AGENT_WEB_PORT` — 默认 `0.0.0.0:8786`

然后：

```bash
python server.py
```

Windows 也可双击 `start.bat`，或：

```bat
set AGENT_WEB_PASSWORD=your-long-random-password
python server.py
```

打开 `http://127.0.0.1:8786/login`。

## Cloudflare Tunnel（公网 HTTPS）

这个应用必须留在 **这台机器** 上（它要调本地 `agent`）。不要当成 Worker 去部署。Windows 服务 `cloudflared` 已经在跑 named tunnel **cursor**（`27a9add3-53d2-45bf-8cfd-b2ba1a3b6853`）。不要再开 Quick Tunnel（`trycloudflare.com`），网址会变，而且不支持 SSE。

在 Zero Trust → Networks → Tunnels → **cursor** → Public Hostname 加一条（只需一次）：

- 子域：`cursor`
- 域名：`klinedance.kdns.fr`（另一个已激活的是 `klinedance.dpdns.org`）
- 类型：HTTP
- URL：`127.0.0.1:8786`

然后保持 `start.bat` 运行，打开 `https://cursor.klinedance.kdns.fr/login`。`tunnel.bat` 只打印状态，不会再起一条隧道。

这等于把能改文件的 Agent 暴露到公网。请保留网页密码，named hostname 上优先开 Cloudflare Access，不要把 URL 随便发给人。

如果 `secrets.json` 是从别的机器拷来的，旧密码哈希仍然有效，直到你设置 `AGENT_WEB_PASSWORD`（会覆盖哈希）。外机 Linux 路径会在启动时映射到本机，旧的 `chat_id` 会被清掉。

## 安全（把端口暴露出去之前先看）

- 默认绑 `0.0.0.0`，同一局域网的手机才能连。流量是 **明文 HTTP**。不要走公共 Wi‑Fi。更稳妥是 SSH 隧道，或 `AGENT_WEB_HOST=127.0.0.1`。
- agent 模式可以在 workspace 里 **读写文件、跑命令**（`--force`）。只想问答就用 ask / plan。
- 永远不要提交 `secrets.json`、`state.json`、`.env`。里面有 API key、密码哈希、会话 cookie。
- 拷走过的 `secrets.json` 里的 API key 应视为已泄露。不确定就去 Cursor 后台轮换。

## 发到 GitHub 前检查

1. 本地把 `.env.example` 复制成 `.env`。不要把真实密钥写进 example。
2. 确认 `.gitignore` 忽略了 `secrets.json`、`state.json`、`.env`。
3. `git status` 里不能出现 API key、密码哈希、会话 token，或别的机器路径（`/mnt/...`、`/workspace/...`）。
4. `secrets.example.json` 和 `state.example.json` 里不要有凭证。
5. 不要提交 `~/.cursor/chats` 下的会话数据库。

## 环境变量

| 变量 | 含义 |
| --- | --- |
| `AGENT_BIN` | `agent` / `agent.exe` / `agent.cmd` 的路径 |
| `AGENT_WORKSPACE` | 默认 workspace |
| `AGENT_WEB_HOST` | 监听地址 |
| `AGENT_WEB_PORT` | 端口（默认 8786） |
| `AGENT_WEB_PASSWORD` | 设置/替换网页登录密码哈希 |
| `CURSOR_API_KEY` | Agent 鉴权（也可在页面里粘贴） |
| `HTTPS_PROXY` / `AGENT_HTTPS_PROXY` | Agent CLI 走的 HTTP 代理（不设则自动用 Clash/系统代理） |
| `CURSOR_CHATS_ROOT` | 覆盖 CLI 会话 sqlite 根目录 |
| `CURSOR_PROJECTS_ROOT` | 覆盖 IDE `~/.cursor/projects` 根目录 |
| `AGENT_WEB_SESSION_DAYS` | Cookie 有效期（默认 7 天） |

## 许可

风险自负。这是对 Cursor CLI 的封装；你需要 Cursor 账号，并遵守他们的条款。
