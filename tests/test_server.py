import json
import io
from contextlib import redirect_stdout

import pytest

from feature_separate_eval_mcp import ledger
from feature_separate_eval_mcp import run_skill_eval
from feature_separate_eval_mcp.server import (
    feature_separate_eval_check,
    feature_separate_eval_run,
    feature_separate_eval_status,
)


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_EVAL_DATA_DIR", str(tmp_path))
    ledger.configure_data_dir(tmp_path)
    yield tmp_path
    ledger.configure_data_dir(None)


def test_status_lists_pending_freetext_record(isolated_data_dir):
    record_id = ledger.record_freetext(
        "一种锂电池，包括集流体。",
        [{"name": "claim_1", "separates": ["一种锂电池", "包括集流体"]}],
    )

    result = feature_separate_eval_status()

    assert result["ok"] is True
    assert result["data"]["pending_records"] == 1
    assert result["data"]["next_record"]["record_id"] == record_id


def test_check_freetext_never_needs_network():
    record_id = ledger.record_freetext(
        "一种锂电池，包括集流体。",
        [{"name": "claim_1", "separates": ["一种锂电池", "包括集流体"]}],
    )

    result = feature_separate_eval_check(record_id=record_id)

    assert result["ok"] is True
    assert result["data"]["ok"] is True
    assert result["data"]["source_claim_count"] == 1


def test_run_refuses_network_without_confirmation():
    record_id = ledger.record_freetext(
        "一种锂电池，包括集流体。",
        [{"name": "claim_1", "separates": ["一种锂电池", "包括集流体"]}],
    )

    result = feature_separate_eval_run(record_id=record_id)

    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert "尚未获得本次发送确认" in result["logs"]


def test_ledger_is_valid_json(isolated_data_dir):
    ledger.record_freetext(
        "技术方案",
        [{"name": "claim_1", "separates": ["技术方案"]}],
    )

    payload = json.loads((isolated_data_dir / ledger.LEDGER_NAME).read_text())

    assert payload["schema_version"] == ledger.SCHEMA_VERSION
    assert len(payload["records"]) == 1


def test_report_stdout_is_resolved_at_call_time():
    item = {
        "record": {"record_id": "r1"},
        "label": "自由文本「测试」",
        "input_kind": ledger.KIND_FREETEXT,
        "claim_results": [],
        "rules_version": "test",
        "judge_model": "test",
    }
    output = io.StringIO()

    with redirect_stdout(output):
        run_skill_eval._print_report(item, [], [], [])

    assert "拆解评测" in output.getvalue()
