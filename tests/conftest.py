"""Shared pytest fixtures that prevent CI hangs when API keys are absent."""

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_run_config_context(monkeypatch, tmp_path):
    """A05: 清理运行级配置快照与按目录的磁盘缓存实例注册表。

    - ContextVar 快照残留会让下一用例读到上一运行的数据源/语言配置；
    - `_SCOPE_DISK_CACHES` 的实例绑定运行快照的 data_cache_dir，残留实例
      会让旧目录（甚至用户真实目录）的磁盘缓存被后续用例复用——重置为
      空表后，新作用域实例一律基于当次用例自己的临时目录构建。
    """
    from tradingagents.dataflows import cache_utils, config as data_config

    data_config._run_config.set(None)
    monkeypatch.setattr(cache_utils, "_SCOPE_DISK_CACHES", {})
    yield
    data_config._run_config.set(None)


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "ZHIPU_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        monkeypatch.setenv(env_var, os.environ.get(env_var, "placeholder"))


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "tradingagents.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client

def _make_eval_result(sub_id: str, trials_data: list) -> dict:
    """Build a minimal valid evaluation result for comparison tests."""
    from tradingagents.evaluation.schema import FIXED_CHECK_IDS

    trials = []
    for td in trials_data:
        checks = [
            {"check_id": cid, "status": "pass" if td.get("pass") else "fail", "detail": "test"}
            for cid in FIXED_CHECK_IDS
        ]
        trials.append({
            "case_id": td["case_id"], "trial_id": td["trial_id"],
            "status": "completed", "contract_pass": td.get("pass", False),
            "checks": checks, "report_digest": "0" * 64,
            "review": {"review_status": "unreviewed", "claims": []},
        })
    expected = len(trials)
    passed = sum(1 for t in trials if t["contract_pass"])
    return {
        "schema_version": 1, "grader_version": "1",
        "suite_id": "test", "suite_version": "1", "suite_digest": "d" * 64,
        "submission_id": sub_id, "trials": trials,
        "metrics": {
            "expected": expected, "provided": len(trials),
            "completed": len(trials), "failed": 0, "invalid": 0,
            "missing": max(0, expected - len(trials)),
            "contract_passed": passed,
            "completion_rate": len(trials) / expected,
            "failure_rate": max(0, expected - len(trials)) / expected,
            "contract_pass_rate": passed / expected,
            "unknown_checks": 0,
            "reviewed_trials": 0, "invalid_reviews": 0, "unreviewed_trials": len(trials),
            "reviewed_claims": 0, "evidence_backed_claims": 0,
            "total_supported": 0, "total_contradicted": 0,
            "total_unverifiable": 0, "total_future_evidence": 0,
            "factual_support_rate": None, "temporal_violation_rate": None,
            "observations": {
                "latency_ms": {"observed_count": 0, "mean": None},
                "input_tokens": {"observed_count": 0, "mean": None},
                "output_tokens": {"observed_count": 0, "mean": None},
                "cost_by_currency": {},
                "_note": "导入测量值，非评分器实测",
            },
        },
    }
