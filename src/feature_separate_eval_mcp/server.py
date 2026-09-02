"""Expose the feature-separation evaluator through MCP tools."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import run_skill_eval


mcp = MCPServer(
    "feature-separate-eval",
    instructions=(
        "Evaluate one recorded patent-tech-feature-separate result. "
        "Call status first, check second, and only run after the user confirms "
        "the disclosed network data scope."
    ),
)


def _invoke(arguments: list[str]) -> dict[str, Any]:
    """Run the proven CLI workflow while keeping stdio clean for MCP JSON-RPC."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = run_skill_eval.main(arguments)
    return {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "output": stdout.getvalue().strip(),
        "logs": stderr.getvalue().strip(),
    }


def _target_args(record_id: str | None, patent_number: str | None) -> list[str]:
    if record_id and patent_number:
        raise ValueError("record_id and patent_number are mutually exclusive")
    if record_id:
        return ["--record-id", record_id]
    if patent_number:
        return ["--patent", patent_number]
    return []


def _decode_json_output(result: dict[str, Any]) -> dict[str, Any]:
    try:
        result["data"] = json.loads(result.pop("output"))
    except (json.JSONDecodeError, TypeError):
        pass
    return result


@mcp.tool()
def feature_separate_eval_status() -> dict[str, Any]:
    """List recorded decompositions and the next pending record; never uses network."""
    return _decode_json_output(_invoke(["--status"]))


@mcp.tool()
def feature_separate_eval_check(
    record_id: str | None = None,
    patent_number: str | None = None,
    allow_network: bool = False,
) -> dict[str, Any]:
    """Check whether one record is evaluable without calling Judge.

    Patent records query only the CLMS field from the configured PatSnap host and
    therefore require allow_network=true after user confirmation. Free-text
    records are checked locally and do not require network permission.
    """
    arguments = ["--check", *_target_args(record_id, patent_number)]
    if allow_network:
        arguments.append("--allow-network")
    return _decode_json_output(_invoke(arguments))


@mcp.tool()
def feature_separate_eval_run(
    record_id: str | None = None,
    patent_number: str | None = None,
    judge_model: str | None = None,
    max_features_per_claim: int = run_skill_eval.GT_MAX_FEATURES_PER_CLAIM,
    max_feature_density: float = run_skill_eval.GT_MAX_FEATURE_DENSITY,
    keep_pending: bool = False,
    allow_network: bool = False,
) -> dict[str, Any]:
    """Evaluate one decomposition and return its human report and persisted result.

    This sends claim text, decomposed features, and deterministic checks to the
    configured Judge. Patent records first send the patent number to PatSnap and
    request only CLMS. Set allow_network=true only after the user confirms this
    exact scope.
    """
    arguments = [
        *_target_args(record_id, patent_number),
        "--max-features-per-claim",
        str(max_features_per_claim),
        "--max-feature-density",
        str(max_feature_density),
    ]
    if judge_model:
        arguments.extend(["--judge-model", judge_model])
    if keep_pending:
        arguments.append("--keep-pending")
    if allow_network:
        arguments.append("--allow-network")

    result = _invoke(arguments)
    result["report"] = result.pop("output")
    match = re.search(r"^审计已落盘:\s*(.+)$", result["logs"], flags=re.MULTILINE)
    if result["ok"] and match:
        path = Path(match.group(1).strip())
        result["evaluation_file"] = str(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            result["evaluation_read_error"] = str(exc)
        else:
            result["evaluation"] = {
                "record_id": payload.get("record_id"),
                "input_kind": payload.get("input_kind"),
                "label": payload.get("label"),
                "judge_model": payload.get("judge_model"),
                "judge_rules_version": payload.get("judge_rules_version"),
                "claim_total": payload.get("claim_total"),
                "findings": payload.get("findings") or [],
                "granularity_outliers": payload.get("granularity_outliers") or [],
                "failures": payload.get("failures") or [],
                "deterministic_aggregate": payload.get("deterministic_aggregate"),
            }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Feature Separate Eval MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        mcp.run(transport=args.transport)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
