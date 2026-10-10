from pathlib import Path
from unittest.mock import patch

import server


def test_position_pnl_summary_separates_paper_and_real():
    rows = [
        {"status": "OPEN", "mode": "PAPER", "floating_pnl": 12.34},
        {"status": "PARTIAL", "mode": "PAPER", "floating_pnl": -2.0},
        {"status": "OPEN", "mode": "REAL", "floating_pnl": 5.5},
        {"status": "CLOSED", "mode": "PAPER", "floating_pnl": 999.0},
    ]
    result = server._position_pnl_summary(rows)
    assert result == {
        "paper_open_count": 2,
        "real_open_count": 1,
        "paper_unrealized_pnl": 10.34,
        "real_unrealized_pnl": 5.5,
        "paper_open_realized_pnl": 0.0,
        "real_open_realized_pnl": 0.0,
    }


def test_dashboard_stats_exposes_moving_paper_session_pnl():
    history = {
        "closed_count": 4,
        "pnl_total": -20.0,
        "paper_pnl": -15.0,
        "real_pnl": -5.0,
        "paper_closed_count": 3,
        "real_closed_count": 1,
        "paper_realized_today": 7.0,
        "real_realized_today": 0.0,
        "paper_closed_today": 1,
        "real_closed_today": 0,
    }
    positions = [
        {"status": "OPEN", "mode": "PAPER", "floating_pnl": 3.25},
        {"status": "OPEN", "mode": "REAL", "floating_pnl": -1.0},
    ]
    with patch.object(server, "_history_summary", return_value=history), \
            patch.object(server, "_engine_positions_snapshot", return_value=positions), \
            patch.object(server, "_engine_broker_reconciliation_snapshot", return_value={"positions": []}):
        stats = server._dashboard_stats()
    assert stats["paper_session_pnl"] == 10.25
    assert stats["paper_unrealized_pnl"] == 3.25
    assert stats["paper_open_count"] == 1
    assert stats["real_open_count"] == 1


def test_dashboard_contains_live_paper_labels_and_position_values():
    html = (Path(server.BASE_DIR) / "dashboard" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "PnL PAPER actual" in html
    assert 'id="stTradesMode"' in html
    assert 'id="tr-pnl-detail"' in html
    assert "Precio actual" in html
    assert "PnL flotante" in html
    assert "stats.paper_session_pnl" in html
