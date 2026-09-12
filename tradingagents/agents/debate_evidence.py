"""E: independent initial views, disagreement planning, bounded news recheck.

Authoritative contract: ``docs/E_CODEX_IMPLEMENTATION_CONTRACT_2026-09-09.md``
(ten rules). E1 = two mutually-blind structured initial views + one
disagreement-planner call (≤3 added logical model calls total); E2 = at most
two actual tool invokes, one per question, news-artifact tools only.

Key semantics baked in (rules 3/6/7):

- Claims with invalid references are KEPT with per-claim validation status —
  never deleted to fake a high compliance rate.
- Reference ID / time validity says NOTHING about semantic support: no
  ``resolved_side`` anywhere; statuses are observable facts only.
- C1 ``evidence_id`` is source-specific — cross-source reposts do not share
  IDs; duplicate CONTENT digests are marked but never counted as votes.
- Model self-reported confidence is always labelled ``uncalibrated``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from tradingagents.agents.utils.agent_utils import get_language_instruction
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.evidence.ledger import (
    SCHEMA_BUNDLE,
    canonicalize_bundle,
    classify_reference,
)
from tradingagents.evidence.prompt_context import evidence_context_for_prompt

SCHEMA_VERSION = 1

RECHECK_MAX_QUESTIONS = 2          # fixed by contract — no config knob
ALLOWED_RECHECK_TOOLS = ("get_news", "get_global_news")
MAX_CLAIMS = 10
MAX_REFS_PER_CLAIM = 5
VIEW_OUTPUT_TOKEN_CAP = 2048       # per logical call (rule 2)

# Hard per-string and per-list text budgets (Codex E R2 #5): persisted state,
# planner prompts and downstream summaries all operate on bounded text —
# truncation is explicit, nothing silently unlimited.
MAX_CLAIM_CHARS = 500
MAX_ASSUMPTION_CHARS = 300
MAX_QUESTION_CHARS = 500
MAX_IMPACT_CHARS = 300
MAX_TOPIC_CHARS = 300
MAX_ID_CHARS = 120
MAX_TOTAL_CLAIM_CHARS = 4000


def _clip(text, limit):
    if not isinstance(text, str) or len(text) <= limit:
        return text, False
    return text[:limit], True

# Hard tool-parameter ceilings (rule 5: model cannot raise them)
PARAM_CEILINGS = {"limit": 20, "look_back_days": 30}

UNCALIBRATED = "uncalibrated"


# ---------------------------------------------------------------------------
# Schemas (structured output of the three logical calls)
# ---------------------------------------------------------------------------


class ViewClaim(BaseModel):
    claim: str = Field(description="One-sentence factual or interpretive claim.")
    evidence_ids: List[str] = Field(
        default_factory=list,
        description="Evidence IDs from the 证据索引 block, verbatim. Empty if none.")
    confidence: str = Field(
        default="medium", description="low | medium | high (self-report only).")


class InitialViewModel(BaseModel):
    direction: str = Field(description="long | short | neutral")
    top_claims: List[ViewClaim] = Field(default_factory=list)
    key_assumptions: List[str] = Field(default_factory=list)
    would_change_mind_if: List[str] = Field(default_factory=list)


class DisagreementItem(BaseModel):
    topic: str
    bull_claim: str = Field(default="", description="The bull side's claim text.")
    bull_evidence_ids: List[str] = Field(default_factory=list)
    bear_claim: str = Field(default="", description="The bear side's claim text.")
    bear_evidence_ids: List[str] = Field(default_factory=list)
    conflict_kind: str = Field(description="fact | interpretation | missing_data")
    period_mismatch: str = Field(
        default="", description="Non-empty when the two sides refer to different periods.")
    decision_impact: str = Field(
        description="How the answer to this disagreement would change the conclusion.")


class QuestionPlan(BaseModel):
    question: str = Field(description="The verifiable question (answerable by "
                                      "get_news / get_global_news).")
    disagreement_index: int = Field(description="Index into the disagreements list.")
    decision_impact: str = Field(
        description="Which answer would change the conclusion (required).")
    tool_name: str = Field(description="One of: get_news, get_global_news.")
    tool_args: Dict[str, Any] = Field(
        description='Complete tool arguments, e.g. '
                    '{"ticker": "600519", "start_date": "2024-10-01", "end_date": "2024-11-05"} '
                    'or {"curr_date": "2024-11-05", "look_back_days": 7, "limit": 10}.')


class DisagreementPlanModel(BaseModel):
    disagreements: List[DisagreementItem] = Field(default_factory=list)
    recheck_questions: List[QuestionPlan] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Usage accounting (rule 2: actual requests counted; unknown ≠ 0)
# ---------------------------------------------------------------------------


class _CapUnavailableError(RuntimeError):
    """The provider cannot enforce the output cap — E refuses to send an
    unbounded request (Codex E R2 #2). Carried to the node, which emits a
    limited record with zero requests."""


class _CountingLLM:
    """Delegate wrapper counting actual invokes + best-effort known tokens.

    Hard requirements (Codex E R1):

    - The 2048-token output cap must reach the ACTUAL HTTP request on every
      path. ``bind().with_structured_output()`` drops the binding on the
      installed stack, so the cap is passed as a kwarg to
      ``with_structured_output(schema, max_tokens=...)`` (verified to reach
      the request payload) and injected into plain ``invoke`` kwargs too —
      falling back only when the provider rejects the kwarg (recorded).
    - B1 retry capability is FORWARDED (``_get_retry_budget``), so an
      explicit user ``max_retries=0`` yields exactly ONE HTTP request under
      the shared budget — the wrapper never widens the budget.
    - The shared client object is never mutated.
    """

    def __init__(self, inner):
        self.inner = inner
        self.invoke_count = 0
        self.known_tokens: Optional[int] = 0
        self.tokens_known = True
        self.cap_applied = True      # cap reached the provider kwargs
        self.cap_enforceable = True  # provider accepted cap kwargs at all

    def _account(self, resp):
        usage = getattr(resp, "usage_metadata", None)
        if isinstance(usage, dict) and usage.get("total_tokens"):
            self.known_tokens = (self.known_tokens or 0) + int(usage["total_tokens"])
        else:
            self.tokens_known = False
        return resp

    def _get_retry_budget(self) -> int:
        # Forward B1 capability — without this the structured helper falls
        # back to the default budget of 3 and explicit max_retries=0 sends
        # three requests.
        if hasattr(self.inner, "_get_retry_budget"):
            try:
                return self.inner._get_retry_budget()
            except Exception:
                pass
        return 3

    def invoke(self, prompt, config=None, **kwargs):
        if not self.cap_enforceable:
            # Refuse BEFORE any request: an unenforceable output cap must not
            # degrade into an uncapped call, and a TypeError may surface only
            # AFTER the request was sent — so we never retry on it.
            raise _CapUnavailableError(
                "provider rejected the max_tokens kwarg — E refuses unbounded requests")
        self.invoke_count += 1
        kwargs.setdefault("max_tokens", VIEW_OUTPUT_TOKEN_CAP)
        try:
            if config is not None:
                return self._account(self.inner.invoke(prompt, config=config, **kwargs))
            return self._account(self.inner.invoke(prompt, **kwargs))
        except TypeError:
            self.cap_applied = False
            self.cap_enforceable = False
            raise _CapUnavailableError(
                "provider rejected max_tokens during invoke — no retry, no uncapped call")

    def with_structured_output(self, schema):
        outer = self

        class _CountingStructured:
            def __init__(self, delegate):
                self._d = delegate

            def invoke(self, prompt, config=None, **kwargs):
                if not outer.cap_enforceable:
                    raise _CapUnavailableError("structured path: cap unenforceable")
                outer.invoke_count += 1
                if config is not None:
                    return outer._account(self._d.invoke(prompt, config=config, **kwargs))
                return outer._account(self._d.invoke(prompt, **kwargs))

        try:
            delegate = self.inner.with_structured_output(schema, max_tokens=VIEW_OUTPUT_TOKEN_CAP)
        except Exception:
            # Capability probe failed BEFORE any request: mark unenforceable
            # and hand out a refusing delegate — never an uncapped fallback.
            outer.cap_applied = False
            outer.cap_enforceable = False

            class _Refusing:
                def invoke(self, prompt, config=None, **kwargs):
                    raise _CapUnavailableError(
                        "provider cannot enforce the E output cap (kwargs rejected)")

            return _Refusing()
        return _CountingStructured(delegate)


def _usage_block(counter: "_CountingLLM", logical_call: str) -> Dict[str, Any]:
    tokens = counter.known_tokens if counter.tokens_known else "unknown"
    return {"logical_call": logical_call,
            "actual_requests": counter.invoke_count,
            "known_tokens": tokens,
            "output_cap_tokens": VIEW_OUTPUT_TOKEN_CAP,
            "cap_applied": counter.cap_applied,
            "cap_enforceable": counter.cap_enforceable}


# ---------------------------------------------------------------------------
# Deterministic validation (rules 3/4)
# ---------------------------------------------------------------------------


def validate_view(view: InitialViewModel, state: Dict[str, Any], position: str) -> Dict[str, Any]:
    """Validate a parsed InitialView into the persisted dict.

    Claims with dangling/future/unknown-time references are KEPT with their
    per-reference verdicts (rule 3) — nothing is deleted to inflate the
    compliance rate. Confidence is relabelled uncalibrated.
    """
    bundle = canonicalize_bundle(state.get("evidence_bundle"))
    if not isinstance(bundle, dict) or bundle.get("schema") != SCHEMA_BUNDLE:
        bundle = None
    cutoff = str(state.get("trade_date") or "")

    claims_out: List[Dict[str, Any]] = []
    truncated_claims = len(view.top_claims) > MAX_CLAIMS
    text_truncated = False
    total_claim_chars = 0
    for c in view.top_claims[:MAX_CLAIMS]:
        refs = [r[:MAX_ID_CHARS] for r in list(c.evidence_ids)[:MAX_REFS_PER_CLAIM]]
        dropped_refs = len(c.evidence_ids) - len(refs)
        verdicts = {r: classify_reference(bundle, r, cutoff)["status"] for r in refs}
        invalid = [r for r, v in verdicts.items() if v != "valid"]
        if not refs:
            status = "no_refs"
        elif invalid:
            status = "has_invalid_refs"
        else:
            status = "all_refs_valid"
        claim_text, clipped = _clip(c.claim, MAX_CLAIM_CHARS)
        text_truncated = text_truncated or clipped
        if total_claim_chars + len(claim_text) > MAX_TOTAL_CLAIM_CHARS:
            room = max(MAX_TOTAL_CLAIM_CHARS - total_claim_chars, 0)
            claim_text = claim_text[:room]
            text_truncated = True
        total_claim_chars += len(claim_text)
        claims_out.append({
            "claim": claim_text,
            "evidence_ids": refs,
            "ref_verdicts": verdicts,
            "invalid_refs": invalid,
            "status": status,
            "confidence": c.confidence,
            "confidence_calibration": UNCALIBRATED,
            **({"truncated_refs": dropped_refs} if dropped_refs else {}),
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "position": position,
        "direction": view.direction,
        "claims": claims_out,
        "key_assumptions": [_clip(a, MAX_ASSUMPTION_CHARS)[0]
                            for a in view.key_assumptions[:10]],
        "would_change_mind_if": [_clip(w, MAX_ASSUMPTION_CHARS)[0]
                                 for w in view.would_change_mind_if[:10]],
        "stats": {
            "claims_total_reported": len(view.top_claims),
            "claims_kept": len(claims_out),
            "claims_truncated": len(view.top_claims) - len(claims_out),
            "all_refs_valid": sum(1 for c in claims_out if c["status"] == "all_refs_valid"),
            "has_invalid_refs": sum(1 for c in claims_out if c["status"] == "has_invalid_refs"),
            "no_refs": sum(1 for c in claims_out if c["status"] == "no_refs"),
        },
        **({"limitations": [lim for keep, lim in (
                (truncated_claims, f"主张数超过 {MAX_CLAIMS}，截断保留前 {MAX_CLAIMS} 条"),
                (text_truncated, f"主张/假设文本超长截断（单条≤{MAX_CLAIM_CHARS}，总量≤{MAX_TOTAL_CLAIM_CHARS}）（truncated）"))
            if keep]} if (truncated_claims or text_truncated) else {}),
    }


_TOOL_ARG_SPECS = {
    "get_news": {"required": ("ticker", "start_date", "end_date"),
                 "optional": {}},
    "get_global_news": {"required": ("curr_date",),
                        "optional": {"look_back_days": int, "limit": int}},
}


def validate_plan(plan: DisagreementPlanModel,
                  allowed_tools=None) -> Dict[str, Any]:
    """Deterministically gate planner output (rule 4).

    First RECHECK_MAX_QUESTIONS eligible questions proceed; the rest are
    ``not_rechecked``. Invalid impact / out-of-bound index / disallowed tool
    / malformed args are ``unresolved`` with explicit reasons — no extra LLM.
    ``allowed_tools`` narrows the tool gate to the run's actual whitelist.
    """
    allowed = tuple(allowed_tools) if allowed_tools is not None else ALLOWED_RECHECK_TOOLS
    disagreements = []
    for d in plan.disagreements[:20]:
        disagreements.append({
            "topic": d.topic,
            "bull_claim": d.bull_claim,
            "bull_evidence_ids": list(d.bull_evidence_ids)[:MAX_REFS_PER_CLAIM],
            "bear_claim": d.bear_claim,
            "bear_evidence_ids": list(d.bear_evidence_ids)[:MAX_REFS_PER_CLAIM],
            "conflict_kind": d.conflict_kind,
            "period_mismatch": d.period_mismatch,
            "decision_impact": d.decision_impact,
        })
    dropped_disagreements = max(len(plan.disagreements) - len(disagreements), 0)

    questions_out: List[Dict[str, Any]] = []
    eligible_count = 0
    for q in plan.recheck_questions:
        record: Dict[str, Any] = {
            "question": q.question,
            "disagreement_index": q.disagreement_index,
            "decision_impact": q.decision_impact,
            "tool_name": q.tool_name,
            "tool_args": dict(q.tool_args or {}),
            "slot": None,
        }
        reason = None
        if not (q.decision_impact or "").strip():
            reason = "unresolved(empty_decision_impact)"
        elif not (0 <= q.disagreement_index < len(disagreements)):
            reason = "unresolved(disagreement_index_out_of_bounds)"
        elif q.tool_name not in allowed:
            reason = "unresolved(tool_not_allowed)"
        elif not _args_conform(q.tool_name, q.tool_args or {}):
            reason = "unresolved(invalid_tool_args)"
        if reason is None and eligible_count >= RECHECK_MAX_QUESTIONS:
            record["status"] = "not_rechecked"
            questions_out.append(record)
            continue
        if reason is None:
            record["slot"] = eligible_count
            record["status"] = "pending"
            eligible_count += 1
        else:
            record["status"] = reason
        questions_out.append(record)

    return {
        "schema_version": SCHEMA_VERSION,
        "disagreements": disagreements,
        "recheck_questions": questions_out,
        "eligible_questions": eligible_count,
        **({"limitations": [f"分歧清单超 20 条截断 {dropped_disagreements} 条"]}
           if dropped_disagreements else {}),
    }


def _args_conform(tool_name: str, args: Dict[str, Any]) -> bool:
    spec = _TOOL_ARG_SPECS.get(tool_name)
    if spec is None or not isinstance(args, dict):
        return False
    for key in spec["required"]:
        if not isinstance(args.get(key), str) or not args.get(key):
            return False
    for key in spec["optional"]:
        if key in args and (isinstance(args[key], bool) or not isinstance(args[key], int)):
            return False
    return set(args) <= set(spec["required"]) | set(spec["optional"])


# ---------------------------------------------------------------------------
# E1 nodes: two mutually-blind initial views + one disagreement planner
# ---------------------------------------------------------------------------


def _view_prompt(position: str, state: Dict[str, Any]) -> str:
    side = "看多（bull）" if position == "bull" else "看空（bear）"
    reports = "\n".join(
        f"{title}:\n{state.get(key, '')}" for key, title in (
            ("market_report", "市场研究"), ("sentiment_report", "情绪分析"),
            ("news_report", "新闻报告"), ("fundamentals_report", "基本面"),
            ("policy_report", "政策分析"), ("hot_money_report", "资金面"),
            ("lockup_report", "解禁/减持"),
        ) if state.get(key))
    evidence = evidence_context_for_prompt(state)
    return f"""你是一位{side}研究员，在与其他研究员辩论**之前**独立形成结构化初判。
只依据下方同一时点的分析师报告与证据索引——此刻你看不到任何对方的观点。

{reports}

{evidence}
要求：
- 最多 {MAX_CLAIMS} 条主张，每条主张引用的证据 ID 必须逐字来自上方证据索引（没有可引用的就留空，不编造）；
- evidence_ids 是来源特定的：不同来源的同一事件是不同 ID，引用你实际依据的那条；
- 区分事实与解读；写下会改变你判断的可观察条件；
- confidence 只是自述标签，系统会统一标注 uncalibrated。{get_language_instruction()}"""


def create_initial_view_node(position: str, llm):
    """One structured initial-view call for one side (mutually blind)."""

    def node(state) -> dict:
        counter = _CountingLLM(llm)
        captured: List[InitialViewModel] = []

        def _render(view: InitialViewModel) -> str:
            if not isinstance(view, InitialViewModel):
                raise ValueError("structured output was empty or not an InitialViewModel")
            captured.append(view)
            return (f"[{position} 初判] direction={view.direction}; "
                    f"claims={len(view.top_claims)}")

        try:
            invoke_structured_or_freetext(
                bind_structured(counter, InitialViewModel, f"{position} initial view"),
                counter, _view_prompt(position, state), _render,
                f"{position} initial view")
        except _CapUnavailableError:
            captured.clear()  # 无界请求已被拒绝——生成受限记录，不伪造主张
        if captured and isinstance(captured[0], InitialViewModel):
            view_dict = validate_view(captured[0], state, position)
            view_dict["output_mode"] = "structured"
        else:
            view_dict = {
                "schema_version": SCHEMA_VERSION, "position": position,
                "direction": "unknown", "claims": [], "key_assumptions": [],
                "would_change_mind_if": [],
                "stats": {"claims_total_reported": 0, "claims_kept": 0,
                          "claims_truncated": 0, "all_refs_valid": 0,
                          "has_invalid_refs": 0, "no_refs": 0},
                "output_mode": "freetext_fallback",
                "limitations": [],  # filled below with the honest reason
            }
            if not counter.cap_enforceable:
                view_dict["limitations"].append(
                    "供应商无法强制输出上限——E 拒绝无界请求，本初判无任何模型输出（不伪造主张）")
            else:
                view_dict["limitations"].append(
                    "结构化输出不可用或返回空/不可解析响应——本次无结构化初判（不伪造）")
        view_dict["usage"] = _usage_block(counter, f"initial_view_{position}")
        # run 绑定（恢复归属校验用；计算本身不感知 run_id）
        view_dict["run_id"] = str((state.get("run_metadata") or {}).get("run_id") or "")
        return {f"initial_view_{position}": view_dict}

    return node


def _view_render_for_planner(view: Optional[Dict[str, Any]], position: str) -> str:
    if not isinstance(view, dict):
        return f"{position}: 无初判（缺失）"
    lines = [f"{position} 初判（direction={view.get('direction')}）："]
    for i, c in enumerate(view.get("claims") or []):
        refs = ", ".join(f"{r}={s}" for r, s in (c.get("ref_verdicts") or {}).items()) or "无引用"
        lines.append(f"  [{i}] {c['claim']}（引用校验: {refs}；主张状态 {c['status']}）")
    for a in view.get("key_assumptions") or []:
        lines.append(f"  假设: {a}")
    return "\n".join(lines)


def create_disagreement_planner_node(llm, allowed_tools=None):
    """One structured planner call over both validated views (E1 third call).

    ``allowed_tools`` is the run's ACTUAL registered whitelist — the prompt
    advertises only what can execute, and validate_plan gates against it
    (no wasted eligible slots on questions whose tool was never enabled).
    """
    allowed = tuple(allowed_tools) if allowed_tools is not None else ALLOWED_RECHECK_TOOLS

    def node(state) -> dict:
        bull = state.get("initial_view_bull")
        bear = state.get("initial_view_bear")
        prompt = f"""你是分歧规划员。下面是两位研究员在互盲条件下形成的结构化初判（引用已附校验状态）。

{_view_render_for_planner(bull, '看多')}
{_view_render_for_planner(bear, '看空')}

产出：
1. disagreements：双方观点真正冲突之处（fact/interpretation/missing_data 分类；若双方所指期间不同必须写 period_mismatch）；每条必须写 decision_impact（该分歧的答案会如何改变结论）。
2. recheck_questions（最多给出 {RECHECK_MAX_QUESTIONS} 条**合格**问题，按影响力排序）：每条给出 question、disagreement_index、decision_impact（哪个答案会改变结论）、tool_name（本次运行实际可用：{'/'.join(allowed) if allowed else '（无可用工具——不要给出问题）'}）与**完整 tool_args**——参数会被代码严格校验后直接执行，不会有人工复核或再规划。
注意：证据 ID 与时点有效只说明"证据可用"，不代表主张被支持——不要据此宣称分歧已解决。{get_language_instruction()}"""
        counter = _CountingLLM(llm)
        captured: List[DisagreementPlanModel] = []

        def _render(plan: DisagreementPlanModel) -> str:
            if not isinstance(plan, DisagreementPlanModel):
                raise ValueError("structured output was empty or not a DisagreementPlanModel")
            captured.append(plan)
            return f"[分歧规划] disagreements={len(plan.disagreements)} questions={len(plan.recheck_questions)}"

        try:
            invoke_structured_or_freetext(
                bind_structured(counter, DisagreementPlanModel, "disagreement planner"),
                counter, prompt, _render, "disagreement planner")
        except _CapUnavailableError:
            captured.clear()
        if captured and isinstance(captured[0], DisagreementPlanModel):
            plan_dict = validate_plan(captured[0], allowed_tools=allowed)
            plan_dict["output_mode"] = "structured"
        else:
            plan_dict = validate_plan(DisagreementPlanModel(), allowed_tools=allowed)
            plan_dict["output_mode"] = "freetext_fallback"
            plan_dict["limitations"] = [
                "供应商无法强制输出上限或结构化输出不可用/为空——E 拒绝无界请求，"
                "本次无分歧规划（不伪造）" if not counter.cap_enforceable else
                "结构化输出不可用或返回空/不可解析响应——本次无分歧规划（不伪造）"]
        plan_dict["usage"] = aggregate_usage(
            state.get("initial_view_bull"), state.get("initial_view_bear"),
            planner_counter=counter)
        run_meta = state.get("run_metadata") or {}
        plan_dict["run_id"] = str(run_meta.get("run_id") or "")
        return {"evidence_debate": plan_dict}

    return node


# ---------------------------------------------------------------------------
# Bounded prompt summary for downstream consumers (rule 9)
# ---------------------------------------------------------------------------


def evidence_debate_summary_for_prompt(state: Any, max_chars: int = 2400) -> str:
    """Bounded E summary for RM/debate prompts ('' when absent).

    Contains only validated facts: view claims with reference verdicts,
    disagreements, question outcomes and known usage. No resolved_side, no
    win-rate language; must not wash out the original data limitations.
    """
    ed = state.get("evidence_debate") if isinstance(state, dict) else None
    if not isinstance(ed, dict) or ed.get("schema_version") != SCHEMA_VERSION:
        return ""
    bull = state.get("initial_view_bull")
    bear = state.get("initial_view_bear")

    def _side(v, name):
        if not isinstance(v, dict):
            return [f"- {name}: 无初判"]
        stats = v.get("stats") or {}
        out = [f"- {name}: direction={v.get('direction')}；"
               f"主张 {stats.get('claims_kept', 0)} 条（引用全有效 {stats.get('all_refs_valid', 0)}/"
               f"含无效引用 {stats.get('has_invalid_refs', 0)}/无引用 {stats.get('no_refs', 0)}；"
               f"confidence=uncalibrated）"]
        for c in (v.get("claims") or [])[:3]:
            out.append(f"  - {name}主张: {str(c.get('claim', ''))[:140]}"
                       f"（{c.get('status', '?')}）")
        return out

    lines = ["独立初判与分歧核查（E 阶段受限摘要——引用/时点有效≠语义支持，无自动裁决）："]
    lines.extend(_side(bull, "看多初判"))
    lines.extend(_side(bear, "看空初判"))
    for i, d in enumerate((ed.get("disagreements") or [])[:8]):
        lines.append(f"- 分歧[{i}] {str(d.get('topic', ''))[:120]}（{d.get('conflict_kind')}）"
                     f"{'；期间不一致: ' + str(d['period_mismatch'])[:80] if d.get('period_mismatch') else ''}"
                     f"——影响: {str(d.get('decision_impact', ''))[:140]}")
    for q in ed.get("recheck_questions") or []:
        outcome = q.get("recheck") or {}
        status = outcome.get("status") or q.get("status", "pending")
        note = outcome.get("note") or q.get("question", "")[:80]
        line = f"- 补查: {status} — {note[:100]}"
        excerpt = str(outcome.get("tool_response_excerpt") or "")[:160].replace("\n", " ")
        if excerpt:
            line += f"｜返回要点: {excerpt}"
        lines.append(line)
    usage = ed.get("usage") or {}
    lines.append(f"- 用量: 逻辑调用 {usage.get('logical_call_count', 'unknown')}/3 上限，"
                 f"工具调用 {usage.get('tool_invokes', 0)}/2 上限"
                 f"（实际请求 {usage.get('actual_request_count', 'unknown')} 次另计，见 JSON）")
    trailer = "\n（E 摘要达字符上限，截断明示；详情见运行 JSON）\n"
    text = "\n".join(lines) + "\n"
    if len(text) > max_chars:
        text = text[:max_chars - len(trailer)] + trailer
    return text[:max_chars]


# ---------------------------------------------------------------------------
# Usage aggregation (rule 2: unknown ≠ 0; actual requests counted separately)
# ---------------------------------------------------------------------------


def aggregate_usage(bull_view, bear_view, planner_counter=None,
                    tool_invokes: Optional[int] = None) -> Dict[str, Any]:
    """Merge per-call usage into the persistent evidence_debate.usage block."""
    parts = {}
    for name, view in (("initial_view_bull", bull_view), ("initial_view_bear", bear_view)):
        u = (view or {}).get("usage") or {}
        parts[name] = {"logical": 1 if (view or {}).get("output_mode") == "structured" else 1,
                       "actual_requests": u.get("actual_requests", "unknown")}
    planner = {"logical": 1,
               "actual_requests": planner_counter.invoke_count if planner_counter else "unknown"}
    logical_count = sum(1 for v in list(parts.values()) + [planner]
                        if v["actual_requests"] != 0)
    # Two INDEPENDENT observability dimensions (Codex E R2 #6): unknown token
    # usage must never erase a known request count, and vice versa.
    tokens_known_list: List[int] = []
    tokens_unknown = False
    for view in (bull_view, bear_view):
        u = (view or {}).get("usage") or {}
        tok = u.get("known_tokens")
        if isinstance(tok, int):
            tokens_known_list.append(tok)
        else:
            tokens_unknown = True  # "unknown" / missing / non-int
    if planner_counter is not None:
        if planner_counter.tokens_known:
            tokens_known_list.append(planner_counter.known_tokens or 0)
        else:
            tokens_unknown = True
    requests_known: List[int] = []
    requests_unknown = False
    for v in list(parts.values()) + [planner]:
        req = v["actual_requests"]
        if isinstance(req, int):
            requests_known.append(req)
        else:
            requests_unknown = False if req == 0 else True
    usage: Dict[str, Any] = {
        "logical_calls": {**parts, "disagreement_planner": planner},
        "logical_call_count": 3,
        "output_cap_tokens": VIEW_OUTPUT_TOKEN_CAP,
        "tool_invokes": 0 if tool_invokes is None else tool_invokes,
    }
    usage["known_tokens"] = "unknown" if tokens_unknown else sum(tokens_known_list)
    usage["actual_request_count"] = ("unknown" if requests_unknown
                                     else sum(requests_known))
    # Honest naming: these are wrapper-observed runnable invokes; plain-path
    # in-process retries mean the true HTTP transport count can only be
    # higher — surfaced as its own explicitly-unknown field, never faked.
    usage["http_request_count"] = "unknown"
    usage["request_count_semantics"] = "wrapper_observed_runnable_invokes"
    return usage


# ---------------------------------------------------------------------------
# E2: bounded recheck executor (deterministic — no LLM)
# ---------------------------------------------------------------------------


def _stable_call_id(slot: int, question: str) -> str:
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:10]
    return f"recheck-q{slot}-{digest}"


def _parse_date_str(value: Any):
    if not isinstance(value, str):
        return None
    try:
        from datetime import datetime as _dt
        return _dt.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


MAX_NEWS_WINDOW_DAYS = 366  # finite stock-window span bound (rule 5)


def _harden_tool_args(tool_name: str, args: Dict[str, Any], state: Dict[str, Any]):
    """Validate + clamp planner args BEFORE any invoke (rules 3/5, E R1).

    Returns ``(effective_args|None, note)`` — None closes the question as
    unresolved WITHOUT invoking anything:

    - trusted instrument: the planner's ticker MUST match the trusted run
      instrument (state.company_of_interest) — a well-formed code for a
      DIFFERENT instrument never reaches the vendor;
    - trusted date: state.trade_date must parse strictly; an unknown
      trusted cutoff can never degrade into an unbounded call;
    - every tool date is strictly parsed; the window is clamped to the
      trusted cutoff and re-validated (start ≤ end; span bounded) —
      invalid/reversed/unbounded windows are rejected pre-invoke;
    - numeric params are int-only (bool never passes) and hard-capped.
    """
    from tradingagents.agents.utils.news_data_tools import _validate_a_stock_code

    args = dict(args or {})
    if not _args_conform(tool_name, args):
        return None, "unresolved(invalid_tool_args)"

    trusted_date = _parse_date_str(state.get("trade_date"))
    if trusted_date is None:
        return None, "unresolved(no_trusted_date)"

    notes = []
    if tool_name == "get_news":
        trusted_instrument = str(state.get("company_of_interest") or "").strip()
        requested = str(args.get("ticker", "")).strip()
        if trusted_instrument and requested != trusted_instrument:
            return None, "unresolved(instrument_mismatch)"
        ok, code = _validate_a_stock_code("get_news", args.get("ticker", ""))
        if not ok:
            return None, "unresolved(invalid_ticker)"
        args["ticker"] = code
        start = _parse_date_str(args.get("start_date"))
        end = _parse_date_str(args.get("end_date"))
        if start is None or end is None:
            return None, "unresolved(invalid_tool_args_dates)"
        if end > trusted_date:
            notes.append("end_date_clamped_to_trusted_cutoff")
            end = trusted_date
        if start > end:
            return None, "unresolved(reversed_window)"
        if (end - start).days + 1 > MAX_NEWS_WINDOW_DAYS:
            from datetime import timedelta as _td
            start = end - _td(days=MAX_NEWS_WINDOW_DAYS - 1)
            notes.append("start_date_narrowed_to_finite_window")
        args["start_date"] = start.isoformat()
        args["end_date"] = end.isoformat()
    else:
        curr = _parse_date_str(args.get("curr_date"))
        if curr is None:
            return None, "unresolved(invalid_tool_args_dates)"
        if curr > trusted_date:
            notes.append("curr_date_clamped_to_trusted_cutoff")
            curr = trusted_date
        args["curr_date"] = curr.isoformat()
        for key in ("look_back_days", "limit"):
            if key in args:
                if isinstance(args[key], bool):
                    return None, "unresolved(invalid_tool_args)"
                try:
                    args[key] = max(1, min(int(args[key]), PARAM_CEILINGS[key]))
                except (TypeError, ValueError):
                    return None, "unresolved(invalid_tool_args)"
    return args, (";".join(notes))


def create_recheck_node(slot: int, evidence_tools: List[Any]):
    """Deterministic single-invoke recheck for one question slot (E2).

    Exactly ONE tool invoke per question (structurally: one constructed
    tool_call into the real ToolNode); budget/slot state persists in
    ``evidence_debate`` so checkpoint resume never re-runs a completed slot
    and never resets counters (rule 8).
    """
    from langchain_core.messages import AIMessage
    from langgraph.prebuilt import ToolNode
    from tradingagents.evidence.cutoff import trusted_cutoff
    from tradingagents.evidence.ledger import collect_tool_message_delta

    tool_node = ToolNode(list(evidence_tools))

    def node(state) -> dict:
        ed = state.get("evidence_debate") or {}
        questions = ed.get("recheck_questions") or []
        question = next((q for q in questions
                         if q.get("slot") == slot and q.get("status") == "pending"), None)
        if question is None:
            return {}  # no eligible question for this slot (or already consumed)
        if question.get("recheck") is not None:
            return {}  # resume window guard: completed slot is never re-run

        def _close(new_ed, status, note, **extra):
            new_ed = dict(new_ed)
            new_questions = [dict(q) for q in new_ed.get("recheck_questions") or []]
            for i, q in enumerate(new_questions):
                if q.get("slot") == slot:
                    q["status"] = "done"
                    q["recheck"] = {"status": status, "note": note,
                                    "human_review": True, **extra}
                    new_questions[i] = q
                    break
            new_ed["recheck_questions"] = new_questions
            return new_ed

        run_id = str((state.get("run_metadata") or {}).get("run_id") or "")
        trade_date = str(state.get("trade_date") or "")
        # 参数收窄必须在可信截止作用域内（clamp_end_date 读调用域 ContextVar），
        # 且作用域覆盖校验+执行全程。
        with trusted_cutoff(trade_date):
            args, note = _harden_tool_args(question.get("tool_name", ""),
                                           question.get("tool_args") or {}, state)
            if args is None:
                return {"evidence_debate": _close(ed, "unresolved", note)}

            call_id = _stable_call_id(slot, question.get("question", ""))
            ai_msg = AIMessage(content="", tool_calls=[{
                "name": question["tool_name"], "args": args, "id": call_id}])
            try:
                out = tool_node.invoke({"messages": [ai_msg]})
            except Exception as exc:  # noqa: BLE001 — 工具面整体失败是可观测状态
                new_ed = _close(ed, "error", f"工具执行异常：{type(exc).__name__}: {exc}")
                new_ed["usage"] = dict(new_ed.get("usage") or {})
                new_ed["usage"]["tool_invokes"] = (new_ed["usage"].get("tool_invokes") or 0) + 1
                return {"evidence_debate": new_ed}
        msgs = (out or {}).get("messages") or []
        delta = collect_tool_message_delta(msgs, role=f"recheck_q{slot}",
                                           run_id=run_id, trade_date=trade_date)
        tool_text = str(getattr(msgs[0], "content", "")) if msgs else ""
        executed = bool(msgs)

        existing = state.get("evidence_bundle") or {}
        existing_digests = {r.get("content_digest")
                            for r in (existing.get("records") or [])}
        new_records = []
        if delta:
            for ev in delta["events"].values():
                new_records.extend((ev.get("evidence_ids") or []))
        # 记录层取回 records：delta events 只带 ids；从 bundle 合并后取
        evidence_ids: List[str] = []
        digests: List[str] = []
        repost_dupes: List[str] = []
        if delta:
            merged_events = delta.get("events") or {}
            for entry in merged_events.values():
                evidence_ids.extend(entry.get("event", {}).get("evidence_ids") or [])
            for rec in _delta_records(delta):
                digests.append(rec.get("content_digest"))
                if rec.get("content_digest") in existing_digests:
                    repost_dupes.append(rec.get("evidence_id"))

        event_statuses = []
        if delta:
            for ev in (delta.get("events") or {}).values():
                event_statuses.append((ev.get("event") or {}).get("status"))
        if not executed:
            status = "error"
            outcome_note = "工具未返回任何消息"
        elif delta is None:
            status = "inconclusive"
            outcome_note = f"工具已执行但无新证据 artifact（返回文本 {len(tool_text)} 字符）"
        elif event_statuses and all(st == "failed" for st in event_statuses):
            status = "error"
            outcome_note = "工具/来源失败（失败细节见 artifact 与证据账本）"
        elif not evidence_ids:
            status = "inconclusive"
            outcome_note = "执行成功但无新证据记录（空/失败来源见 artifact）"
        else:
            status = "retrieved"
            outcome_note = f"新增证据 {len(evidence_ids)} 条（重复内容已标记、不投票）"

        new_ed = _close(ed, status, outcome_note,
                        tool_call_id=call_id,
                        hardening_note=note,
                        effective_args=args,
                        evidence_ids=evidence_ids,
                        content_digests=digests,
                        repost_duplicates=repost_dupes,
                        tool_response_excerpt=tool_text[:400])
        new_ed["usage"] = dict(new_ed.get("usage") or {})
        new_ed["usage"]["tool_invokes"] = (new_ed["usage"].get("tool_invokes") or 0) + 1
        result: Dict[str, Any] = {"evidence_debate": new_ed}
        if delta is not None:
            result["evidence_bundle"] = delta
        return result

    return node


def _delta_records(delta: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for entry in (delta.get("events") or {}).values():
        out.extend(entry.get("records") or [])
    return out


# ---------------------------------------------------------------------------
# Shared Markdown render for Web / MD / PDF (rule 9)
# ---------------------------------------------------------------------------


def render_evidence_debate_md(state: Any) -> str:
    """三出口共用渲染：双初判、分歧与校验、补查结果、用量。

    旧报告/默认关闭 → 未记录（不凭空生成）。无 resolved_side、无胜率表述。
    """
    bull = state.get("initial_view_bull") if isinstance(state, dict) else None
    bear = state.get("initial_view_bear") if isinstance(state, dict) else None
    ed = state.get("evidence_debate") if isinstance(state, dict) else None
    if not isinstance(ed, dict) or ed.get("schema_version") != SCHEMA_VERSION:
        return ("**独立初判与分歧核查**: 未记录（此报告由旧版本生成，或本次运行未启用 "
                "evidence_debate_enabled）。（引用/时点有效≠语义支持；不含自动裁决）")

    def _side(view, name):
        if not isinstance(view, dict):
            return f"- {name}: 无初判（缺失）"
        stats = view.get("stats") or {}
        lines = [f"- {name}: direction={view.get('direction')}（confidence=uncalibrated；"
                 f"主张 {stats.get('claims_kept', 0)} 条：引用全有效 "
                 f"{stats.get('all_refs_valid', 0)}/含无效 {stats.get('has_invalid_refs', 0)}/"
                 f"无引用 {stats.get('no_refs', 0)}）"]
        for c in (view.get("claims") or [])[:10]:
            verdict = "、".join(f"{r}={s}" for r, s in (c.get("ref_verdicts") or {}).items()) or "无引用"
            lines.append(f"  - {c['claim']}（{verdict}）")
        for lim in view.get("limitations") or []:
            lines.append(f"  - 限制: {lim}")
        return "\n".join(lines)

    lines = ["**独立初判与分歧核查**（互盲初判 → 分歧规划 → 有界补查；引用/时点有效≠语义支持，无自动裁决）：",
             _side(bull, "看多初判"), _side(bear, "看空初判"),
             "- 分歧清单:"]
    for i, d in enumerate(ed.get("disagreements") or []):
        lines.append(f"  - [{i}] {d.get('topic')}（{d.get('conflict_kind')}）"
                     f"{'；期间不一致: ' + d['period_mismatch'] if d.get('period_mismatch') else ''}"
                     f"——影响: {d.get('decision_impact', '')[:120]}")
    lines.append("- 补查问题:")
    for q in ed.get("recheck_questions") or []:
        r = q.get("recheck") or {}
        lines.append(f"  - {q.get('status')}（{r.get('status', '-')}）{q.get('question', '')[:100]}"
                     f"{'；证据: ' + ', '.join(r.get('evidence_ids', [])[:5]) if r.get('evidence_ids') else ''}"
                     f"{'；重复内容已标记不投票: ' + str(len(r.get('repost_duplicates', []))) + ' 条' if r.get('repost_duplicates') else ''}")
    usage = ed.get("usage") or {}
    lines.append(f"- 用量: 逻辑调用 {usage.get('logical_call_count', 'unknown')}/3；"
                 f"工具调用 {usage.get('tool_invokes', 0)}/2；"
                 f"实际请求 {usage.get('actual_request_count', 'unknown')}；"
                 f"已知 tokens {usage.get('known_tokens', 'unknown')}（上限为输出约束 2048/调用，不含输入）")
    lines.append("> 未解决/不确定的分歧保留为待人工复核；本节为契约性记录，不代表投资准确率。")
    return "\n".join(lines)
