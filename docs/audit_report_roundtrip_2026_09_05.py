"""Independent report-content contract checks; no UI, font rendering or network."""
from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest

from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph
from web import history, pdf_export
from web.components import report_viewer


@pytest.mark.parametrize("mode", ["live", "saved", "legacy", "both_aliases"])
@pytest.mark.parametrize("surface", ["markdown", "pdf_sections", "web"])
def test_report_content_survives_all_surfaces(monkeypatch, tmp_path, mode, surface):
    state = Propagator().create_initial_state("600519", "2026-01-15")
    state.update(trader_investment_plan="AUDIT_TRADER", data_quality_summary="AUDIT_QUALITY",
                 investment_plan="AUDIT_RESEARCH", final_trade_decision="Rating: Buy\nAUDIT_PORTFOLIO")
    if mode == "saved":
        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"results_dir": str(tmp_path)}
        graph.ticker, graph.log_states_dict = "600519", {}
        graph._log_state("2026-01-15", state)
        state = history.load_analysis(str(next(tmp_path.rglob("full_states_log_*.json"))))
    elif mode == "legacy":
        state["trader_investment_decision"] = state.pop("trader_investment_plan")
    elif mode == "both_aliases":
        state["trader_investment_decision"] = "LEGACY_ALIAS_MUST_NOT_WIN"

    if surface == "markdown":
        text = pdf_export.generate_markdown(state, "600519", "2026-01-15", "Buy")
    elif surface == "pdf_sections":
        # Use the actual generate_pdf entry and collect the sections it sends
        # to its renderer. Font/layout correctness is outside this contract.
        renderer = MagicMock()
        renderer.output.return_value = b"offline PDF renderer"
        monkeypatch.setattr(pdf_export, "_ReportPDF", MagicMock(return_value=renderer))
        pdf_export.generate_pdf(state, "600519", "2026-01-15", "Buy")
        text = "\n".join(str(call.args) for call in renderer.add_section.call_args_list)
    else:
        ui = MagicMock()
        ui.session_state = {}
        ui.columns.side_effect = lambda cols: [nullcontext() for _ in cols]
        ui.tabs.side_effect = lambda tabs: [nullcontext() for _ in tabs]
        ui.expander.side_effect = lambda *a, **kw: nullcontext()
        monkeypatch.setattr(report_viewer, "st", ui)
        monkeypatch.setattr(report_viewer, "_cached_generate_pdf", lambda *a: b"offline")
        monkeypatch.setattr(report_viewer, "_cached_generate_markdown", pdf_export.generate_markdown)
        report_viewer.render_report(state, "600519", "2026-01-15", "Buy")
        text = "\n".join(str(call.args[0]) for call in ui.markdown.call_args_list)

    missing = [marker for marker in ("AUDIT_TRADER", "AUDIT_QUALITY", "AUDIT_RESEARCH", "AUDIT_PORTFOLIO")
               if marker not in text]
    assert not missing, {"mode": mode, "surface": surface, "missing": missing}
    assert text.count("AUDIT_TRADER") == 1
    assert "LEGACY_ALIAS_MUST_NOT_WIN" not in text
