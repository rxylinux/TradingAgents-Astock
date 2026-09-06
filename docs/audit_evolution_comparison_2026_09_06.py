"""Independent N03 behavior checks, including real offline CLI subprocesses."""
import copy
import importlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from tradingagents.agents.report_quality import assess_reports
from tradingagents.run_records import new_run_metadata


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    denied = Mock(side_effect=AssertionError("Unexpected network in pure report comparison"))
    monkeypatch.setattr(requests.Session, "request", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def report(run="a", date="2026-01-15", model="AUDIT_MODEL_A", ticker="600519", kind="stock"):
    config = {"llm_provider": "glm", "deep_think_llm": model, "selected_analysts": ["market"],
              "instrument_type": kind}
    meta = new_run_metadata(config, ticker, date)
    meta.update(run_id=run * 32, created_at=f"2026-01-15T0{1 if run == 'a' else 2}:00:00+00:00")
    state = {"company_of_interest": ticker, "trade_date": date, "run_metadata": meta,
             "selected_analysts": ["market"], "market_report": "AUDIT_MARKET_" + run,
             "trader_investment_plan": "AUDIT_CANONICAL_" + run,
             "final_trade_decision": "Rating: Buy\nAUDIT_FINAL_" + run}
    state["data_quality"] = assess_reports(state, ["market"])
    return state


def module():
    return importlib.import_module("tradingagents.report_comparison")


def rendered(left, right):
    api = module()
    return api.render_comparison_markdown(api.compare_reports(left, right))


def test_comparison_is_pure_and_exposes_changed_conditions_and_content():
    left, right = report(), report("b", "2026-01-16", "AUDIT_MODEL_B")
    right["final_trade_decision"] = "Rating: Sell\nAUDIT_FINAL_b"
    before = copy.deepcopy((left, right))
    api = module()
    result = api.compare_reports(left, right)
    assert api.compare_reports(left, right) == result
    assert (left, right) == before
    text = api.render_comparison_markdown(result)
    for token in ("600519", "2026-01-15", "2026-01-16", "AUDIT_MODEL_A", "AUDIT_MODEL_B",
                  "AUDIT_MARKET_a", "AUDIT_MARKET_b", "AUDIT_FINAL_a", "AUDIT_FINAL_b", "Buy", "Sell"):
        assert token in text, token


@pytest.mark.parametrize("legacy", [False, True])
def test_trader_alias_is_compared_once_with_canonical_priority(legacy):
    left, right = report(), report("b")
    if legacy:
        left["trader_investment_decision"] = left.pop("trader_investment_plan")
    else:
        left["trader_investment_decision"] = "AUDIT_IGNORED_LEGACY_SECRET"
        right["trader_investment_decision"] = "AUDIT_IGNORED_LEGACY_SECRET"
    text = rendered(left, right)
    assert "AUDIT_CANONICAL_a" in text and "AUDIT_CANONICAL_b" in text
    assert "AUDIT_IGNORED_LEGACY_SECRET" not in text


@pytest.mark.parametrize("ticker,kind", [("000002", "stock"), ("600519", "index")])
def test_cross_security_or_instrument_type_is_rejected(ticker, kind):
    with pytest.raises(ValueError):
        module().compare_reports(report(), report("b", ticker=ticker, kind=kind))


def test_legacy_unknown_never_defaults_to_passed_quality_or_hold_rating():
    left = {"company_of_interest": "600519", "trade_date": "2026-01-15", "market_report": ""}
    right = {**left, "trade_date": "2026-01-16"}
    text = rendered(left, right)
    assert any(t in text for t in ("未知", "未记录", "unknown"))
    assert any(t in text for t in ("N/A", "未知", "未记录"))
    assert "Hold" not in text and "全部通过" not in text


def test_enabled_empty_report_is_distinguished_from_inactive_role():
    left, right = report(), report("b")
    right["data_quality"] = assess_reports(right, ["market", "news"])
    right["run_metadata"]["config_snapshot"]["selected_analysts"] = ["market", "news"]
    right["selected_analysts"] = ["market", "news"]
    right["news_report"] = ""
    text = rendered(left, right)
    assert any(t in text for t in ("未启用", "未运行", "inactive", "not_enabled"))
    assert any(t in text for t in ("空", "empty"))


def cli(tmp_path, left, right, *args):
    root = Path(__file__).resolve().parents[1]
    a, b = tmp_path / "left.json", tmp_path / "right.json"
    a.write_text(json.dumps(left, ensure_ascii=False), encoding="utf-8")
    b.write_text(json.dumps(right, ensure_ascii=False), encoding="utf-8")
    # Real subprocess with socket use forbidden, not a mock of the CLI entry.
    (tmp_path / "sitecustomize.py").write_text(
        "import socket\n"
        "def deny(*a, **kw): raise AssertionError('AUDIT_CLI_NETWORK_FORBIDDEN')\n"
        "socket.socket.connect = deny\nsocket.create_connection = deny\n"
    )
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL") if k in os.environ}
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(root)])
    before = a.read_bytes(), b.read_bytes()
    run = subprocess.run([sys.executable, "-m", "tradingagents.report_comparison", str(a), str(b), *args],
                         cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    assert (a.read_bytes(), b.read_bytes()) == before, "CLI modified source reports"
    assert "AUDIT_CLI_NETWORK_FORBIDDEN" not in run.stdout + run.stderr
    return run


@pytest.mark.parametrize("to_file", [False, True])
def test_actual_cli_outputs_comparison_without_model_or_network(tmp_path, to_file):
    target = tmp_path / "comparison.md"
    run = cli(tmp_path, report(), report("b", model="AUDIT_MODEL_B"),
              *( ["--output", str(target)] if to_file else []))
    assert run.returncode == 0, run.stderr
    text = target.read_text(encoding="utf-8") if to_file else run.stdout
    assert "AUDIT_MODEL_A" in text and "AUDIT_MODEL_B" in text
    assert "AUDIT_MARKET_a" in text and "AUDIT_MARKET_b" in text


@pytest.mark.parametrize("invalid", ["different_ticker", "wrong_shape"])
def test_actual_cli_errors_do_not_publish_success_output(tmp_path, invalid):
    right = report("b", ticker="000002") if invalid == "different_ticker" else ["invalid report"]
    target = tmp_path / "comparison.md"
    run = cli(tmp_path, report(), right, "--output", str(target))
    assert run.returncode != 0
    assert not target.exists()


def test_cli_output_cannot_overwrite_an_input_report(tmp_path):
    run = cli(tmp_path, report(), report("b"), "--output", str(tmp_path / "left.json"))
    assert run.returncode != 0


@pytest.mark.parametrize("field", ["market_report", "trader_investment_plan", "final_trade_decision"])
def test_downloadable_comparison_preserves_late_changes_in_long_reports(field):
    left, right = report(), report("b")
    left[field] = "\n".join(f"old line {i}" for i in range(160)) + "\nAUDIT_LAST_LEFT"
    right[field] = "\n".join(f"new line {i}" for i in range(160)) + "\nAUDIT_LAST_RIGHT"
    text = rendered(left, right)
    assert "AUDIT_LAST_LEFT" in text and "AUDIT_LAST_RIGHT" in text, "Download silently lost late report differences"


def test_old_index_reports_are_not_relabelled_as_stocks():
    left = {"company_of_interest": "000001.SH", "trade_date": "2026-01-15"}
    right = {**left, "trade_date": "2026-01-16"}
    result = module().compare_reports(left, right)
    assert result["instrument_type"] != "stock", "Old index reports were fabricated as stock reports"


def test_unknown_quality_and_unknown_team_do_not_become_known_empty():
    left = {"company_of_interest": "600519", "trade_date": "2026-01-15",
            "data_quality": assess_reports({}, [])}
    right = report("b")
    result = module().compare_reports(left, right)
    assert not result["quality"]["diff"]["comparable"]
    for change in result["analyst_changes"]:
        assert "已启用" not in change["left_label"] and change["left_label"] != "未运行"
