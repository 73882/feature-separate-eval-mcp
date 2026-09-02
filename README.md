# feature-separate-eval-mcp

把 `feature-separate-eval` 的 Python 后端抽成独立 MCP server。Skill 本身只保留流程与安全边界，实际调用以下 MCP tools：

- `feature_separate_eval_status`：列出拆解记录，零网络调用。
- `feature_separate_eval_check`：检查记录、原文和 claim 对齐；专利记录只回取 `CLMS`。
- `feature_separate_eval_run`：执行确定性检查、粒度闸门和一次 Judge 审计。

服务支持本地 STDIO 和 Streamable HTTP。两种模式都使用同一套后端逻辑。

## 本地运行

需要 Python 3.10+：

```bash
python3.10 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
```

编辑 `.env`。`CC_EVAL_DATA_DIR` 必须与 `patent-tech-feature-separate` 使用同一个绝对路径；不要提交真实 token。

STDIO：

```bash
.venv/bin/feature-separate-eval-mcp
```

Streamable HTTP：

```bash
.venv/bin/feature-separate-eval-mcp --transport streamable-http
```

HTTP MCP endpoint 默认为 `http://127.0.0.1:8000/mcp`。

## 连接 Claude Code

公开仓库可以直接克隆，不需要 GitHub token：

```bash
git clone https://github.com/73882/feature-separate-eval-mcp.git
cd feature-separate-eval-mcp
python3.10 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
```

在 `.env` 中配置网关与共享 ledger 路径后，用绝对路径注册用户级
STDIO server：

```bash
claude mcp add --scope user --transport stdio feature-separate-eval -- \
  /absolute/path/feature-separate-eval-mcp/.venv/bin/feature-separate-eval-mcp
claude mcp list
```

重新打开 Claude Code，在 `/mcp` 中确认 server 为 connected。Claude Code
也支持把配置写进项目根 `.mcp.json`；用户级注册避免把本机绝对路径提交到项目。
官方说明：<https://code.claude.com/docs/en/mcp>。

## 旧 `.env` 迁移

原项目里的变量分为两组：

- MCP 使用：`PATSNAP_API_BASE`、Judge 变量以及新增的
  `CC_EVAL_DATA_DIR`、两个精确 host 白名单。
- MCP 不使用：`REAL_TARGET_URL`、`AGENT_WORK_DIR`、`PROXY_LOG_DIR`；它们属于
  Claude 代理或原专利客户端的文件输出配置。

若内部网关只有 HTTP，必须显式配置：

```dotenv
CC_EVAL_ALLOW_INSECURE_HTTP=true
CLAIM_DECOMPOSITION_JUDGE_ALLOWED_HOSTS=judge.internal.example
PATSNAP_ALLOWED_HOSTS=patent.internal.example
```

只允许填写 URL 中的精确 hostname。公网端点保持该开关为 `false` 并使用 HTTPS。
`PATSNAP_KEY` 在内部端点不要求鉴权时可以为空。Judge token 仍为必填。
`CLAIM_DECOMPOSITION_JUDGE_TOKEN_PREFIX="Bearer "` 与 `Bearer` 都会规范为一个空格，
无需保留尾随空格。

不要把真实 token 提交到 Git；`.env` 已被 `.gitignore` 排除。若 token 曾出现在聊天、
日志或提交中，应立即吊销并换新。

## 连接 Codex（可选）

Codex 支持 STDIO 与 Streamable HTTP，并从 `~/.codex/config.toml` 或受信项目的
`.codex/config.toml` 读取配置：<https://developers.openai.com/codex/mcp>。

远程 HTTP 示例：

```toml
[mcp_servers.feature-separate-eval]
url = "https://your-host.example/mcp"
tool_timeout_sec = 300
required = true
```

公开部署必须在反向代理或 MCP OAuth 层完成鉴权；不要把无鉴权的评测端点暴露到公网。

## 数据与安全边界

- `status` 只读 ledger，不联网。
- `check` 对自由文本完全本地；对专利只向白名单 PatSnap host 发送专利号并请求 `CLMS`。
- `run` 向 Judge 发送当前记录的 claim 原文、拆解特征和确定性检查结果。
- 所有上游 URL 必须命中精确 host allowlist。默认只允许 HTTPS；可信内网 HTTP
  必须通过 `CC_EVAL_ALLOW_INSECURE_HTTP=true` 显式开启。
- MCP tool 参数 `allow_network` 默认为 `false`；调用方必须先向用户说明发送范围并取得确认。

## 测试

```bash
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```
