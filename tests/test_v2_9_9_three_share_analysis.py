from __future__ import annotations

from pathlib import Path

from core.ticker_analysis import build_ticker_accuracy_analysis, partial_share_plan
from core.risk_manager import GestorDeRiesgo


def _close(date, ticker, pnl, qty=3, entry=100.0, mode="PAPER"):
    return {
        "fecha": date,
        "ticker": ticker,
        "evento": "POSITION_CLOSE",
        "pnl_usd": pnl,
        "cantidad": qty,
        "entrada": entry,
        "modo": mode,
    }


def test_three_shares_are_split_two_plus_one_for_partial_exit():
    plan = partial_share_plan(3, 0.70, True)
    assert plan["enabled"] is True
    assert plan["partial_shares"] == 2
    assert plan["runner_shares"] == 1
    assert plan["actual_partial_pct"] == 0.6667

    no_partial = partial_share_plan(2, 0.70, True)
    assert no_partial["enabled"] is False
    assert no_partial["partial_shares"] == 0
    assert no_partial["runner_shares"] == 2


def test_engine_risk_manager_matches_three_share_plan():
    manager = GestorDeRiesgo.__new__(GestorDeRiesgo)
    manager.pct_primer_objetivo = 0.70
    assert manager._repartir_cantidad(3, True) == (2, 1)
    assert manager._repartir_cantidad(2, True) == (2, 0)


def test_accuracy_ranking_penalizes_small_samples_and_uses_actual_closes():
    rows = []
    # TSLA: 12 decisive closes, 9 wins / 3 losses, positive normalized result.
    for i in range(9):
        rows.append(_close("2026-08-22", "TSLA", 6.0, qty=3, entry=300.0))
    for i in range(3):
        rows.append(_close("2026-08-22", "TSLA", -4.0, qty=3, entry=300.0))

    # AAPL: 12 closes but weaker win rate.
    for i in range(7):
        rows.append(_close("2026-08-22", "AAPL", 5.0, qty=3, entry=200.0))
    for i in range(5):
        rows.append(_close("2026-08-22", "AAPL", -4.0, qty=3, entry=200.0))

    # BA: 100% raw win rate but only 2 closes -> must NOT outrank eligible tickers.
    rows += [
        _close("2026-08-22", "BA", 10.0, qty=3, entry=220.0),
        _close("2026-08-22", "BA", 8.0, qty=3, entry=220.0),
    ]

    analysis = build_ticker_accuracy_analysis(
        ["AAPL", "BA", "TSLA"],
        history_rows=rows,
        as_of_date="2026-08-23",
        lookback_days=30,
        min_closed_trades=10,
        ai_rows=[
            {"ticker": "TSLA", "last_balanced_accuracy": 0.61, "last_incumbent_balanced_accuracy": 0.57},
            {"ticker": "BA", "last_balanced_accuracy": 0.75, "last_incumbent_balanced_accuracy": 0.44},
        ],
    )
    by = {row["ticker"]: row for row in analysis}

    assert by["TSLA"]["sample_eligible"] is True
    assert by["TSLA"]["rank"] == 1
    assert by["TSLA"]["win_rate_pct"] == 75.0
    assert by["TSLA"]["review_candidate"] is True

    assert by["AAPL"]["sample_eligible"] is True
    assert by["AAPL"]["rank"] == 2

    assert by["BA"]["win_rate_pct"] == 100.0
    assert by["BA"]["sample_eligible"] is False
    assert by["BA"]["rank"] is None
    assert by["BA"]["model_candidate_ba_pct"] == 75.0


def test_accuracy_analysis_separates_paper_real_and_normalizes_share_size():
    rows = [
        _close("2026-08-21", "META", 6.0, qty=1, entry=500.0, mode="REAL"),
        _close("2026-08-22", "META", -2.0, qty=20, entry=500.0, mode="PAPER"),
    ]
    analysis = build_ticker_accuracy_analysis(
        ["META"], history_rows=rows, as_of_date="2026-08-23",
        lookback_days=30, min_closed_trades=1,
        execution_rows=[{"ticker": "META", "effective_mode": "REAL", "shares": 3}],
    )
    row = analysis[0]
    assert row["paper_closed"] == 1
    assert row["real_closed"] == 1
    assert row["paper_pnl"] == -2.0
    assert row["real_pnl"] == 6.0
    assert row["configured_shares"] == 3
    assert row["shares_closed"] == 21
    assert row["net_return_pct"] != 0


def test_v299_dashboard_and_server_expose_analysis_and_three_share_plan():
    root = Path(__file__).resolve().parents[1]
    html = (root / "dashboard" / "index.html").read_text(encoding="utf-8")
    server = (root / "server.py").read_text(encoding="utf-8")
    version = (root / "VERSION").read_text(encoding="utf-8").strip()

    assert tuple(int(x) for x in version.split(".")) >= (2, 9, 9)
    assert "Análisis automático de precisión por ticker" in html
    assert "fetchTickerAnalysis" in html
    assert "partialPlanForShares" in html
    assert "Plan parcial" in html
    assert "/api/ticker-analysis" in server
    assert "partial_share_plan" in server
