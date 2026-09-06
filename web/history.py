"""Manage completed and incomplete analysis history."""

from __future__ import annotations

import json
import logging
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from tradingagents.default_config import DEFAULT_CONFIG


logger = logging.getLogger(__name__)

_INCOMPLETE_TASKS_FILE = Path.home() / ".tradingagents" / "incomplete_tasks.json"
_INCOMPLETE_TASKS_LOCK = threading.Lock()


def _results_dir() -> Path:
    """A12: 历史列表与写入端共用同一配置源。

    写入端（``TradingAgentsGraph._log_state``）落在运行配置的 results_dir
    （``TRADINGAGENTS_RESULTS_DIR`` 环境变量优先，见 DEFAULT_CONFIG）；
    读取端固定扫 ``~/.tradingagents/logs`` 会让自定义输出目录里的报告从
    不出现在历史列表。这里读取 DEFAULT_CONFIG 当前值，与环境变量/默认
    目录的优先级保持一致。
    """
    return Path(DEFAULT_CONFIG["results_dir"])


_LOG_NAME_RE = re.compile(r"full_states_log_(\d{4}-\d{2}-\d{2})\.json$")


_READ_ERROR = object()  # 哨兵：文件不可解析（调用方应整条跳过）


def _read_run_info(path: Path):
    """读取报告 JSON 的 (run_id, created_at)。

    返回 ``(None, None)`` 表示可解析但没有运行档案（旧报告）；返回
    ``_READ_ERROR`` 表示文件畸形——调用方**整条跳过**（不以目录名伪装身
    份入列），记 warning 诊断，不拖垮整个历史列表（N02）。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except UnicodeDecodeError as e:
        logger.warning("历史记录 %s 非法 UTF-8（已跳过）: %s", path, e)
        return _READ_ERROR
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("历史记录 %s 无法解析（已跳过）: %s", path, e)
        return _READ_ERROR
    if not isinstance(data, dict):
        logger.warning("历史记录 %s 结构异常（已跳过）", path)
        return _READ_ERROR
    meta = data.get("run_metadata")
    run_id = None
    created_at = None
    if isinstance(meta, dict):
        run_id = meta.get("run_id")
        created_at = meta.get("created_at")
    return run_id, created_at


def get_history() -> list[dict[str, Any]]:
    """Scan saved analysis logs and return a sorted list (newest first).

    N02: 同时扫描版本存档（``_runs/<run_id>/<ticker>/…``，同日多次运行
    各自保留）与最新兼容入口（``<ticker>/…``）。同一 run_id 的版本文件
    与兼容文件在列表只显示一次（优先版本路径——身份完整）；旧的无
    metadata 报告继续可见。

    Each entry: {"ticker", "date", "path", "run_id", "created_at"}；
    旧报告 run_id/created_at 为空串。排序：date 降序，同日按 created_at
    降序（无时间者排最后）。
    """
    root = _results_dir()
    if not root.exists():
        return []

    entries: list[dict[str, Any]] = []
    seen_keys: set[tuple] = set()

    def _add(log_file: Path, ticker: str, date: str, run_id, created_at, prefer: bool):
        # 去重键：有 run_id 按 run_id（兼容文件与版本文件同 run 只显示一次，
        # 版本路径优先——先加入者优先）；旧文件按规范化路径。
        key = ("run", str(run_id)) if run_id else ("path", str(log_file))
        if key in seen_keys:
            return
        seen_keys.add(key)
        entries.append({
            "ticker": ticker,
            "date": date,
            "path": str(log_file),
            "run_id": str(run_id or ""),
            "created_at": str(created_at or ""),
        })

    # 1) 版本存档：_runs/<run_id>/<ticker>/TradingAgentsStrategy_logs/…
    runs_root = root / "_runs"
    if runs_root.exists():
        for log_file in sorted(runs_root.glob("*/*/TradingAgentsStrategy_logs/full_states_log_*.json")):
            match = _LOG_NAME_RE.search(log_file.name)
            if not match:
                continue
            date = match.group(1)
            run_id = log_file.parents[2].name
            ticker = log_file.parents[1].name
            info = _read_run_info(log_file)
            if info is _READ_ERROR:
                continue  # 畸形文件：整条跳过（不以目录名伪装身份）
            rid_from_meta, created_at = info
            # 身份以文件内容为准；目录名与内容不一致时以内容为主并告警
            if rid_from_meta and rid_from_meta != run_id:
                logger.warning(
                    "版本目录 run_id=%s 与报告内容 run_id=%s 不一致（%s）",
                    run_id, rid_from_meta, log_file,
                )
            _add(log_file, ticker, date, rid_from_meta or run_id, created_at, prefer=True)

    # 2) 最新兼容入口：<ticker>/TradingAgentsStrategy_logs/…（不进入 _runs）
    for log_file in sorted(root.glob("*/TradingAgentsStrategy_logs/full_states_log_*.json")):
        match = _LOG_NAME_RE.search(log_file.name)
        if not match:
            continue
        date = match.group(1)
        ticker = log_file.parent.parent.name
        info = _read_run_info(log_file)
        if info is _READ_ERROR:
            continue
        run_id, created_at = info
        _add(log_file, ticker, date, run_id, created_at, prefer=False)

    entries.sort(
        key=lambda e: (e["date"], e.get("created_at") or ""),
        reverse=True,
    )
    return entries


def _completed_key(ticker: str, trade_date: str) -> tuple[str, str]:
    return ticker.upper(), trade_date


def _completed_keys() -> set[tuple[str, str]]:
    return {
        _completed_key(entry["ticker"], entry["date"])
        for entry in get_history()
    }


def _load_incomplete_index() -> list[dict[str, Any]]:
    if not _INCOMPLETE_TASKS_FILE.exists():
        return []

    try:
        with open(_INCOMPLETE_TASKS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (UnicodeDecodeError, OSError, json.JSONDecodeError):
        return []

    if not isinstance(data, list):
        return []

    entries: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker", "")).strip().upper()
        trade_date = str(item.get("trade_date", "")).strip()
        if not ticker or not re.match(r"^\d{4}-\d{2}-\d{2}$", trade_date):
            continue
        item["ticker"] = ticker
        item["trade_date"] = trade_date
        entries.append(item)
    return entries


def _save_incomplete_index(entries: list[dict[str, Any]]) -> None:
    """原子写 incomplete_tasks.json，兼容 Windows 文件占用。

    目标文件可能被其他进程短暂占用（如多实例 Web UI、杀毒软件扫描），
    此时 ``tmp.replace`` 在 Windows 上会抛 ``PermissionError``（#77）。
    先重试几次等待锁释放，仍失败则降级为直接覆写——读取端
    （``_load_incomplete_index``）已容错损坏 JSON，索引写不进去不致命。
    """
    parent = _INCOMPLETE_TASKS_FILE.parent
    parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(entries, ensure_ascii=False, indent=2)

    for attempt in range(3):
        tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=parent,
                prefix=f"{_INCOMPLETE_TASKS_FILE.stem}.",
                suffix=".tmp",
                delete=False,
            ) as f:
                f.write(payload)
                tmp = Path(f.name)
            tmp.replace(_INCOMPLETE_TASKS_FILE)
            return
        except PermissionError:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            if attempt < 2:
                # 锁通常是瞬时的，短暂等待后重试
                time.sleep(0.15 * (attempt + 1))
        except OSError:
            raise

    # 重试耗尽仍被占用：直接覆写（非原子但可接受）。
    try:
        _INCOMPLETE_TASKS_FILE.write_text(payload, encoding="utf-8")
    except OSError as e:
        # 索引写不进去不致命——读取端容错、下次写入会自动重建，所以不往上抛。
        # 但**不能一声不吭**：完全静默的话，用户永远不会知道它一直在失败，
        # 「未完成任务」列表长期不更新时也无从排查。
        logger.warning(
            "写入未完成任务索引失败（已重试并降级为直接覆写）：%s。"
            "不影响本次分析，但侧边栏的未完成任务列表可能不是最新的。", e
        )


def _checkpoint_step(ticker: str, trade_date: str) -> int | None:
    try:
        from tradingagents.graph.checkpointer import checkpoint_step

        return checkpoint_step(DEFAULT_CONFIG["data_cache_dir"], ticker, trade_date)
    except Exception:
        return None


def record_incomplete_task(
    ticker: str,
    trade_date: str,
    *,
    status: str,
    error: str | None = None,
    completed_stages: list[str] | None = None,
) -> None:
    """Upsert a resumable task entry."""
    ticker = ticker.strip().upper()
    trade_date = trade_date.strip()
    if not ticker or not trade_date:
        return

    with _INCOMPLETE_TASKS_LOCK:
        entries = [
            entry
            for entry in _load_incomplete_index()
            if _completed_key(entry["ticker"], entry["trade_date"])
            != _completed_key(ticker, trade_date)
        ]
        now = time.time()
        entries.append(
            {
                "ticker": ticker,
                "trade_date": trade_date,
                "status": status,
                "error": error or "",
                "completed_stages": completed_stages or [],
                "updated_at": now,
            }
        )
        entries.sort(key=lambda e: float(e.get("updated_at", 0)), reverse=True)
        _save_incomplete_index(entries)


def clear_incomplete_task(ticker: str, trade_date: str) -> None:
    """Remove an incomplete task once it completes successfully."""
    ticker = ticker.strip().upper()
    trade_date = trade_date.strip()
    with _INCOMPLETE_TASKS_LOCK:
        entries = [
            entry
            for entry in _load_incomplete_index()
            if _completed_key(entry["ticker"], entry["trade_date"])
            != _completed_key(ticker, trade_date)
        ]
        _save_incomplete_index(entries)


def get_incomplete_history() -> list[dict[str, Any]]:
    """Return unfinished tasks that can be resumed from their checkpoint.

    A11: 同标的同日期的**旧完成报告**不得隐藏/删除新失败任务的恢复入口
    ——fresh 重跑只清断点与索引、不删旧报告，旧报告的存在不能证明"这一
    次"运行已完成。区分依据是**断点存在性**（本次运行身份的可恢复证据）：

    - 有有效断点 → 保留（新失败任务可从断点续跑，无论旧报告是否存在）；
    - 无断点且已有完成报告 → 旧条目对应的运行确实完成过（或断点已被成功
      收尾清理），作为陈旧条目过滤；
    - 无断点也无报告 → 保留（展示失败原因；无恢复点但信息本身有用）。
    """
    completed = _completed_keys()
    active_entries: list[dict[str, Any]] = []

    with _INCOMPLETE_TASKS_LOCK:
        entries = _load_incomplete_index()
        for entry in entries:
            key = _completed_key(entry["ticker"], entry["trade_date"])

            step = _checkpoint_step(entry["ticker"], entry["trade_date"])
            entry["checkpoint_step"] = step
            if step is None and key in completed:
                continue
            active_entries.append(entry)

        active_entries.sort(key=lambda e: float(e.get("updated_at", 0)), reverse=True)
        if len(active_entries) != len(entries):
            _save_incomplete_index(active_entries)
    return active_entries


def load_analysis(path: str) -> dict[str, Any]:
    """Load a saved analysis JSON file."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def extract_signal(state: dict[str, Any]) -> str:
    """Extract the 5-tier rating from a final state dict for history reload.

    Delegates to the shared ``parse_rating`` heuristic so the history-reload
    display matches the live signal (``TradingAgentsGraph.process_signal``) and
    understands Chinese free-text decisions — not just English keywords. The
    old English-only ``BUY/SELL/HOLD`` scan silently returned Hold/N/A for
    every Chinese-output run (issues #78 / #80). ``final_trade_decision`` is
    checked first so the reload matches the authoritative live signal.
    """
    import re

    from tradingagents.agents.utils.rating import parse_rating

    _UNKNOWN = ""
    for field in (
        "final_trade_decision",
        "trader_investment_decision",
        "investment_plan",
    ):
        text = state.get(field, "")
        if not text:
            continue
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        rating = parse_rating(cleaned, default=_UNKNOWN)
        if rating:
            return rating
    return "N/A"
