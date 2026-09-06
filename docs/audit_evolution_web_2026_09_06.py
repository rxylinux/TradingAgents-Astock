"""Real Streamlit state transitions with temporary historical reports only."""
import json
from pathlib import Path
from unittest.mock import Mock

import dotenv
import pytest
import requests
from streamlit.testing.v1 import AppTest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.run_records import new_run_metadata
from web import history


@pytest.fixture
def app(monkeypatch, tmp_path):
    denied = Mock(side_effect=AssertionError("Unexpected remote request in Web audit"))
    monkeypatch.setattr(requests.Session, "request", denied)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path))
    monkeypatch.setitem(DEFAULT_CONFIG, "data_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", tmp_path / "incomplete.json")
    from web.components import report_viewer
    monkeypatch.setattr(report_viewer, "_cached_generate_pdf", lambda *a: b"offline")
    for number, letter in enumerate("abc"):
        day = f"2026-01-{15 + number}"
        meta = new_run_metadata({"deep_think_llm": f"AUDIT_MODEL_{letter}"}, "600519", day)
        meta["run_id"] = letter * 32
        state = {"company_of_interest": "600519", "trade_date": day, "run_metadata": meta,
                 "final_trade_decision": f"Rating: Buy\nAUDIT_BODY_{letter}", "market_report": f"AUDIT_MARKET_{letter}"}
        target = tmp_path / "_runs" / meta["run_id"] / "600519/TradingAgentsStrategy_logs" / f"full_states_log_{day}.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(state), encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    at = AppTest.from_file(str(root / "web/app.py"), default_timeout=15).run()
    assert not at.exception
    return at


def compare_button(at):
    return next(b for b in at.button if "对比两份" in b.label)


def choose(at, key, index):
    # Support either historical-index or stable-path widget values, while
    # asserting the user-visible result independently below.
    box = at.selectbox(key=key)
    value = index if isinstance(box.value, int) else history.get_history()[index]["path"]
    box.select(value).run()
    assert not at.exception


def test_comparison_result_is_cleared_when_selection_changes_or_is_invalid(app):
    at = app
    compare_button(at).click().run()
    assert at.selectbox(key="cmp_select_left").value != at.selectbox(key="cmp_select_right").value
    choose(at, "cmp_select_left", 0)
    choose(at, "cmp_select_right", 1)
    at.button(key="cmp_run_button").click().run()
    assert not at.exception
    assert len(at.get("download_button")) == 1
    choose(at, "cmp_select_right", 2)
    assert len(at.get("download_button")) == 0, "Download still represents the previously selected pair"
    at.button(key="cmp_run_button").click().run()
    assert len(at.get("download_button")) == 1
    source = Path(history.get_history()[2]["path"])
    changed = json.loads(source.read_text())
    changed["company_of_interest"] = "000002"
    source.write_text(json.dumps(changed))
    at.button(key="cmp_run_button").click().run()
    assert at.error
    assert len(at.get("download_button")) == 0, "Comparison error left an old result downloadable"
    choose(at, "cmp_select_right", 0)
    at.button(key="cmp_run_button").click().run()
    assert at.error
    assert len(at.get("download_button")) == 0, "Same-report error left an unrelated comparison downloadable"


def test_single_history_report_can_enter_compare_and_return_to_original(app):
    at = app
    next(b for b in at.sidebar.button if "运行 cccccccc" in b.label).click().run()
    assert not at.exception
    assert any("AUDIT_BODY_c" in m.value for m in at.markdown)
    compare_button(at).click().run()
    assert at.selectbox(key="cmp_select_left")
    at.button(key="cmp_back_button").click().run()
    assert not at.exception
    assert any("AUDIT_BODY_c" in m.value for m in at.markdown)


def test_invalid_encoding_history_does_not_hide_valid_versions(monkeypatch, tmp_path):
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path))
    folder = tmp_path / "600519/TradingAgentsStrategy_logs"
    folder.mkdir(parents=True)
    (folder / "full_states_log_2026-01-15.json").write_text('{"company_of_interest":"600519"}')
    (folder / "full_states_log_2026-01-16.json").write_bytes(b"\xff\xfe")
    rows = history.get_history()
    assert len(rows) == 1 and rows[0]["date"] == "2026-01-15"


def test_history_compare_entry_is_unavailable_while_analysis_runs(app):
    from web.progress import ProgressTracker
    at = app
    next(b for b in at.sidebar.button if "运行 cccccccc" in b.label).click().run()
    at.session_state["tracker"] = ProgressTracker(ticker="600519", trade_date="2026-01-20", is_running=True)
    at.run()
    assert not at.exception
    assert not any("对比两份" in b.label and not b.disabled for b in at.button)
