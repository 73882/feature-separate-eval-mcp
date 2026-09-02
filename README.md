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

## 连接 Codex

Codex 官方文档说明，本地客户端支持 STDIO 与 Streamable HTTP，并从 `~/.codex/config.toml` 或受信项目的 `.codex/config.toml` 读取 MCP 配置：<https://developers.openai.com/codex/mcp>。

本地 STDIO 示例：

```toml
[mcp_servers.feature-separate-eval]
command = "/absolute/path/feature-separate-eval-mcp/.venv/bin/feature-separate-eval-mcp"
cwd = "/absolute/path/feature-separate-eval-mcp"
tool_timeout_sec = 300
required = true

[mcp_servers.feature-separate-eval.env]
CC_EVAL_DATA_DIR = "/absolute/path/to/cc_eval_data"
```

其余网关配置可写在仓库 `.env`，也可继续放入上述 `env` 表。

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
- 所有上游 URL 必须是 HTTPS、命中精确 host allowlist、禁止重定向并校验 TLS。
- MCP tool 参数 `allow_network` 默认为 `false`；调用方必须先向用户说明发送范围并取得确认。

## 测试

```bash
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```
