"""Append-only markdown decision log for TradingAgents."""

from typing import List, Optional
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
import os
import re
import threading

from tradingagents.agents.utils.file_lock import file_lock
from tradingagents.agents.utils.rating import parse_rating


class TradingMemoryLog:
    """Append-only markdown log of trading decisions and reflections.

    A06: every read-modify-write (append / single & batch outcome backfill /
    rotation) and every read runs inside one cross-instance, cross-thread,
    cross-process transaction (advisory file lock on ``<log>.lock``). Without
    it, an append landing between another writer's read and its atomic
    ``os.replace`` is silently lost. Reflections (LLM calls) stay OUTSIDE the
    lock — callers generate them first and only then submit the batch.
    """

    # HTML comment: cannot appear in LLM prose output, safe as a hard delimiter
    _SEPARATOR = "\n\n<!-- ENTRY_END -->\n\n"
    # Precompiled patterns — avoids re-compilation on every load_entries() call
    _DECISION_RE = re.compile(r"DECISION:\n(.*?)(?=\nREFLECTION:|\Z)", re.DOTALL)
    _REFLECTION_RE = re.compile(r"REFLECTION:\n(.*?)$", re.DOTALL)
    # as_of 模式下收益/复盘不可注入时的统一说明，让模型知道这不是数据遗漏
    _OUTCOME_HIDDEN_NOTE = "（该笔收益与复盘晚于分析时点或缺乏时间元数据，未注入）"

    def __init__(self, config: dict = None):
        cfg = config or {}
        self._log_path = None
        path = cfg.get("memory_log_path")
        if path:
            # resolve：经符号链接打开的日志读写**真实文件**——否则原子
            # os.replace 会把符号链接本身替换成一个独立新文件，真实日志
            # 从未被更新（Codex 退回修正）。
            self._log_path = Path(path).expanduser().resolve()
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # Optional cap on resolved entries. None disables rotation.
        self._max_entries = cfg.get("memory_log_max_entries")

    # --- Transaction boundary (A06) ---

    @contextmanager
    def _transaction(self):
        """Serialize all access to the log file across instances/threads/processes.

        No-op when this instance has no log path. Public entry points take the
        lock exactly once and use ``_*_unlocked`` helpers internally — never
        call another public method while holding the lock (deadlock).
        """
        if not self._log_path:
            yield
            return
        with file_lock(self._log_path):
            yield

    def _read_entries_unlocked(self) -> List[dict]:
        """Read + parse entries. Caller must hold ``_transaction``."""
        if not self._log_path or not self._log_path.exists():
            return []
        text = self._log_path.read_text(encoding="utf-8")
        raw_entries = [e.strip() for e in text.split(self._SEPARATOR) if e.strip()]
        entries = []
        for raw in raw_entries:
            parsed = self._parse_entry(raw)
            if parsed:
                entries.append(parsed)
        return entries

    def _atomic_write_unlocked(self, new_text: str) -> None:
        """Atomically replace the log. Caller must hold ``_transaction``.

        临时文件名含 pid + 线程 id：两个并发写者即使绕过锁（防御）也不会
        互相覆盖/替换对方的临时文件；``os.replace`` 保证读者只见旧或新。
        """
        tmp_path = self._log_path.with_suffix(
            f".tmp.{os.getpid()}.{threading.get_ident()}"
        )
        tmp_path.write_text(new_text, encoding="utf-8")
        os.replace(tmp_path, self._log_path)

    # --- Write path (Phase A) ---

    def store_decision(
        self,
        ticker: str,
        trade_date: str,
        final_trade_decision: str,
    ) -> None:
        """Append pending entry at end of propagate(). No LLM call."""
        if not self._log_path:
            return
        with self._transaction():
            # Idempotency guard: fast raw-text scan instead of full parse.
            # 幂等检查与追加必须在同一事务内：锁外的「检查→追加」窗口会
            # 让两个并发重复决策都通过检查、写入两条相同 pending。
            if self._log_path.exists():
                raw = self._log_path.read_text(encoding="utf-8")
                for line in raw.splitlines():
                    if (
                        line.startswith(f"[{trade_date} | {ticker} |")
                        and line.endswith("| pending]")
                    ):
                        return
            rating = parse_rating(final_trade_decision)
            tag = f"[{trade_date} | {ticker} | {rating} | pending]"
            entry = f"{tag}\n\nDECISION:\n{final_trade_decision}{self._SEPARATOR}"
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(entry)
                f.flush()
                os.fsync(f.fileno())

    # --- Read path (Phase A) ---

    def load_entries(self) -> List[dict]:
        """Parse all entries from log. Returns list of dicts."""
        with self._transaction():
            return self._read_entries_unlocked()

    def get_pending_entries(self) -> List[dict]:
        """Return entries with outcome:pending (for Phase B)."""
        return [e for e in self.load_entries() if e.get("pending")]

    def get_past_context(
        self,
        ticker: str,
        n_same: int = 5,
        n_cross: int = 3,
        as_of: Optional[str] = None,
    ) -> str:
        """Return formatted past context string for agent prompt injection.

        ``as_of`` is the analysis point-in-time (trade_date, interpreted as
        *after that day's close*). When given, entries are filtered so a
        historical analysis never reads decisions, outcomes, or reflections
        that did not exist yet (R4/F3). Outcome figures and reflection text
        have **separate** knowability rules:

        - decision date > as_of          → entry completely hidden;
        - outcome window end (``end=``) <= as_of → return figures injectable;
        - reflection generated at (``resolved=``) <= as_of → reflection
          text injectable — a reflection written today must not leak into an
          earlier backtest date even when the price window already closed;
        - missing/invalid time metadata → that piece is withheld (an
          unprovable fact is treated as future information).

        ``as_of=None`` keeps the live-analysis behaviour (everything resolved
        so far is injectable).
        """
        entries = [e for e in self.load_entries() if not e.get("pending")]
        if not entries:
            return ""

        as_of_date = self._parse_date(as_of) if as_of is not None else None
        if as_of is not None and as_of_date is None:
            # 无法确定分析时点就什么都不注入：宁可缺失记忆，不可泄漏未来
            return ""

        same, cross = [], []
        for e in reversed(entries):
            if len(same) >= n_same and len(cross) >= n_cross:
                break
            visible_outcome = True
            visible_reflection = True
            if as_of_date is not None:
                vis = self._visibility_as_of(e, as_of_date)
                if vis == "hidden":
                    continue
                visible_outcome = vis in ("full", "outcome_only")
                visible_reflection = vis == "full"
            if e["ticker"] == ticker and len(same) < n_same:
                same.append((e, visible_outcome, visible_reflection))
            elif e["ticker"] != ticker and len(cross) < n_cross:
                cross.append((e, visible_outcome, visible_reflection))

        if not same and not cross:
            return ""

        parts = []
        if same:
            parts.append(f"Past analyses of {ticker} (most recent first):")
            parts.extend(
                self._format_full(e, include_outcome=o, include_reflection=r)
                for e, o, r in same
            )
        if cross:
            parts.append("Recent cross-ticker lessons:")
            parts.extend(
                self._format_reflection_only(e, include_outcome=o, include_reflection=r)
                for e, o, r in cross
            )
        return "\n\n".join(parts)

    def find_short_window_outcomes(
        self, min_effective_days: int = 5
    ) -> List[dict]:
        """诊断（只读）：列出可能被旧版缩短窗口结算的 outcomes。

        A08 之前的实现用 ``min(holding_days, len(stock)-1, len(bench)-1)``
        静默缩短评估窗口并立即定稿——这些条目 holding 标记小于默认 5 日，
        其收益/复盘对应的实际期限取决于用户当时何时重跑，样本口径不一。
        本方法只识别不修改；受影响条目的处置见迁移说明（由用户显式决定，
        不自动改写历史记忆）。
        """
        flagged = []
        for e in self.load_entries():
            if e.get("pending"):
                continue
            holding = str(e.get("holding") or "").strip().rstrip("d")
            try:
                days = int(holding)
            except ValueError:
                continue
            if days < min_effective_days:
                flagged.append(e)
        return flagged

    # --- Update path (Phase B) ---

    def update_with_outcome(
        self,
        ticker: str,
        trade_date: str,
        raw_return: float,
        alpha_return: float,
        holding_days: int,
        reflection: str,
        outcome_end: Optional[str] = None,
    ) -> None:
        """Replace pending tag and append REFLECTION section using atomic write.

        Finds the first pending entry matching (trade_date, ticker), updates
        its tag with return figures, and appends a REFLECTION section.  Uses
        a temp-file + os.replace() so a crash mid-write never corrupts the log.

        ``outcome_end`` is the actual last trading date of the return window
        (from market data, not calendar arithmetic). Together with the
        resolution date it lets future historical analyses prove when the
        outcome/reflection became knowable (R4).
        """
        if not self._log_path or not self._log_path.exists():
            return

        with self._transaction():
            text = self._log_path.read_text(encoding="utf-8")
            blocks = text.split(self._SEPARATOR)

            pending_prefix = f"[{trade_date} | {ticker} |"
            raw_pct = f"{raw_return:+.1%}"
            alpha_pct = f"{alpha_return:+.1%}"

            updated = False
            new_blocks = []
            for block in blocks:
                stripped = block.strip()
                if not stripped:
                    new_blocks.append(block)
                    continue

                lines = stripped.splitlines()
                tag_line = lines[0].strip()

                # 提交时重读核对：仍处于 pending 才结算（并发/重复回填安全）
                if (
                    not updated
                    and tag_line.startswith(pending_prefix)
                    and tag_line.endswith("| pending]")
                ):
                    # Parse rating from the existing pending tag
                    fields = [f.strip() for f in tag_line[1:-1].split("|")]
                    rating = fields[2]
                    new_tag = self._resolved_tag(
                        trade_date, ticker, rating, raw_pct, alpha_pct,
                        holding_days, outcome_end,
                    )
                    rest = "\n".join(lines[1:])
                    new_blocks.append(
                        f"{new_tag}\n\n{rest.lstrip()}\n\nREFLECTION:\n{reflection}"
                    )
                    updated = True
                else:
                    new_blocks.append(block)

            if not updated:
                return

            new_blocks = self._apply_rotation(new_blocks)
            self._atomic_write_unlocked(self._SEPARATOR.join(new_blocks))

    def batch_update_with_outcomes(self, updates: List[dict]) -> None:
        """Apply multiple outcome updates in a single read + atomic write.

        Each element of updates must have keys: ticker, trade_date,
        raw_return, alpha_return, holding_days, reflection.  Optional key
        ``outcome_end`` records the return window's actual end date (R4).

        A06: 整个读-改-写（含轮转）在同一事务内；仅当条目在提交时刻仍为
        pending 才结算。反思文本由调用方在锁外生成后传入。
        """
        if not self._log_path or not self._log_path.exists() or not updates:
            return

        with self._transaction():
            text = self._log_path.read_text(encoding="utf-8")
            blocks = text.split(self._SEPARATOR)

            # Build lookup keyed by (trade_date, ticker) for O(1) dispatch
            update_map = {(u["trade_date"], u["ticker"]): u for u in updates}

            new_blocks = []
            for block in blocks:
                stripped = block.strip()
                if not stripped:
                    new_blocks.append(block)
                    continue

                lines = stripped.splitlines()
                tag_line = lines[0].strip()

                matched = False
                for (trade_date, ticker), upd in list(update_map.items()):
                    pending_prefix = f"[{trade_date} | {ticker} |"
                    if tag_line.startswith(pending_prefix) and tag_line.endswith("| pending]"):
                        fields = [f.strip() for f in tag_line[1:-1].split("|")]
                        rating = fields[2]
                        raw_pct = f"{upd['raw_return']:+.1%}"
                        alpha_pct = f"{upd['alpha_return']:+.1%}"
                        new_tag = self._resolved_tag(
                            trade_date, ticker, rating, raw_pct, alpha_pct,
                            upd["holding_days"], upd.get("outcome_end"),
                        )
                        rest = "\n".join(lines[1:])
                        new_blocks.append(
                            f"{new_tag}\n\n{rest.lstrip()}\n\nREFLECTION:\n{upd['reflection']}"
                        )
                        del update_map[(trade_date, ticker)]
                        matched = True
                        break

                if not matched:
                    new_blocks.append(block)

            new_blocks = self._apply_rotation(new_blocks)
            self._atomic_write_unlocked(self._SEPARATOR.join(new_blocks))

    # --- Helpers ---

    @staticmethod
    def _resolved_tag(trade_date, ticker, rating, raw_pct, alpha_pct,
                      holding_days, outcome_end) -> str:
        """Build a resolved tag, appending outcome time metadata when known."""
        tag = (
            f"[{trade_date} | {ticker} | {rating}"
            f" | {raw_pct} | {alpha_pct} | {holding_days}d"
        )
        if outcome_end:
            tag += f" | end={outcome_end}"
        tag += f" | resolved={datetime.now().strftime('%Y-%m-%d')}"
        return tag + "]"

    @staticmethod
    def _parse_date(value) -> Optional[object]:
        """Parse 'YYYY-MM-DD' (optionally with time) to a date; None on failure."""
        if not value:
            return None
        try:
            return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    def _visibility_as_of(self, entry: dict, as_of_date) -> str:
        """Classify an entry's injectability at an analysis point-in-time.

        Returns:
        - "hidden": the decision itself postdates the analysis (or its date
          is unparseable) — nothing is injectable;
        - "full": outcome window closed at/before as_of **and** the
          reflection was generated at/before as_of — everything injectable;
        - "outcome_only": return figures are provably knowable, but the
          reflection was written later (or cannot be proven) — figures
          injectable, reflection withheld (F3);
        - "decision_only": neither the outcome nor the reflection is
          provably knowable — decision text only.
        """
        decision_date = self._parse_date(entry.get("date"))
        if decision_date is None or decision_date > as_of_date:
            return "hidden"
        outcome_end = self._parse_date(entry.get("outcome_end"))
        resolved_at = self._parse_date(entry.get("resolved_at"))
        outcome_ok = outcome_end is not None and outcome_end <= as_of_date
        # 复盘文本的实际生成时间是 resolved_at：它晚于收益窗口结束
        # （回填在下一次运行才发生），不能拿收益已发生证明复盘当时已存在
        reflection_ok = resolved_at is not None and resolved_at <= as_of_date
        if outcome_ok and reflection_ok:
            return "full"
        if outcome_ok:
            return "outcome_only"
        return "decision_only"

    def _apply_rotation(self, blocks: List[str]) -> List[str]:
        """Drop oldest resolved blocks when their count exceeds max_entries.

        Pending blocks are always kept (they represent unprocessed work).
        Returns ``blocks`` unchanged when rotation is disabled or under cap.
        """
        if not self._max_entries or self._max_entries <= 0:
            return blocks

        # Tag each block with (kept, is_resolved) by parsing tag-line markers.
        decisions = []
        for block in blocks:
            stripped = block.strip()
            if not stripped:
                decisions.append((block, False))
                continue
            tag_line = stripped.splitlines()[0].strip()
            is_resolved = (
                tag_line.startswith("[")
                and tag_line.endswith("]")
                and not tag_line.endswith("| pending]")
            )
            decisions.append((block, is_resolved))

        resolved_count = sum(1 for _, r in decisions if r)
        if resolved_count <= self._max_entries:
            return blocks

        to_drop = resolved_count - self._max_entries
        kept: List[str] = []
        for block, is_resolved in decisions:
            if is_resolved and to_drop > 0:
                to_drop -= 1
                continue
            kept.append(block)
        return kept

    def _parse_entry(self, raw: str) -> Optional[dict]:
        lines = raw.strip().splitlines()
        if not lines:
            return None
        tag_line = lines[0].strip()
        if not (tag_line.startswith("[") and tag_line.endswith("]")):
            return None
        fields = [f.strip() for f in tag_line[1:-1].split("|")]
        if len(fields) < 4:
            return None
        entry = {
            "date": fields[0],
            "ticker": fields[1],
            "rating": fields[2],
            "pending": fields[3] == "pending",
            "raw": fields[3] if fields[3] != "pending" else None,
            "alpha": fields[4] if len(fields) > 4 else None,
            "holding": fields[5] if len(fields) > 5 else None,
            # R4 时间元数据：end=收益窗口实际结束日，resolved=回填发生日。
            # 旧格式条目两者皆为 None —— as_of 模式按保守规则处理。
            "outcome_end": None,
            "resolved_at": None,
        }
        for field in fields[6:]:
            if field.startswith("end="):
                entry["outcome_end"] = field[len("end="):]
            elif field.startswith("resolved="):
                entry["resolved_at"] = field[len("resolved="):]
        body = "\n".join(lines[1:]).strip()
        decision_match = self._DECISION_RE.search(body)
        reflection_match = self._REFLECTION_RE.search(body)
        entry["decision"] = decision_match.group(1).strip() if decision_match else ""
        entry["reflection"] = reflection_match.group(1).strip() if reflection_match else ""
        return entry

    def _format_full(
        self,
        e: dict,
        include_outcome: bool = True,
        include_reflection: bool = True,
    ) -> str:
        if not include_outcome:
            tag = f"[{e['date']} | {e['ticker']} | {e['rating']}]"
            return f"{tag}\nDECISION:\n{e['decision']}\n{self._OUTCOME_HIDDEN_NOTE}"
        raw = e["raw"] or "n/a"
        alpha = e["alpha"] or "n/a"
        holding = e["holding"] or "n/a"
        meta = ""
        if e.get("outcome_end"):
            meta = f" | end={e['outcome_end']}"
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {raw} | {alpha} | {holding}{meta}]"
        parts = [tag, f"DECISION:\n{e['decision']}"]
        if e["reflection"] and include_reflection:
            parts.append(f"REFLECTION:\n{e['reflection']}")
        elif e["reflection"]:
            parts.append(
                "（复盘生成时间晚于分析时点，未注入）"
            )
        return "\n\n".join(parts)

    def _format_reflection_only(
        self,
        e: dict,
        include_outcome: bool = True,
        include_reflection: bool = True,
    ) -> str:
        if not include_outcome:
            text = e["decision"][:300]
            suffix = "..." if len(e["decision"]) > 300 else ""
            return (
                f"[{e['date']} | {e['ticker']} | {e['rating']}]\n{text}{suffix}\n"
                f"{self._OUTCOME_HIDDEN_NOTE}"
            )
        tag = f"[{e['date']} | {e['ticker']} | {e['rating']} | {e['raw'] or 'n/a'}]"
        if e["reflection"] and include_reflection:
            return f"{tag}\n{e['reflection']}"
        if e["reflection"]:
            return f"{tag}\n（复盘生成时间晚于分析时点，未注入）"
        text = e["decision"][:300]
        suffix = "..." if len(e["decision"]) > 300 else ""
        return f"{tag}\n{text}{suffix}"
