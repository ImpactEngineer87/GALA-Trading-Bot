"""Read-only explanation of session P/L from verified display evidence."""
from datetime import datetime, timezone

from core.dashboard_evidence import mode_of, number, session_date as row_date
from core.ticker_performance import build_ticker_performance


def build_pnl_breakdown(history_rows, positions, session_date):
    closed = [r for r in history_rows if r.get('evento') == 'POSITION_CLOSE'
              and row_date(r) == session_date]
    active = [r for r in positions if r.get('status') in {'OPEN', 'PARTIAL'}]
    tickers = sorted({str(r.get('ticker') or '') for r in closed + active} - {''})
    performance = build_ticker_performance(tickers, history_rows=closed,
        positions=active, session_date=session_date)
    modes = {}
    for mode in ('PAPER', 'REAL'):
        rows = [r for r in closed if mode_of(r) == mode]
        eligible = [r for r in rows if r.get('performance_verified') is True
                    and number(r.get('pnl_usd')) is not None]
        intentional = [r for r in rows if r.get('performance_intentionally_excluded') is True
                       and number(r.get('pnl_usd')) is not None and r not in eligible]
        unknown = [r for r in rows if r not in eligible and r not in intentional]
        opened = [r for r in active if mode_of(r) == mode]
        unknown_open = [r for r in opened if mode == 'REAL'
            or number(r.get('floating_pnl')) is None or r.get('valuation_available') is False]
        known_closed = round(sum(float(r['pnl_usd']) for r in eligible), 2)
        known_open = round(sum(float(r['floating_pnl']) for r in opened if r not in unknown_open), 2)
        realized = None if unknown else known_closed
        unrealized = None if unknown_open else known_open
        net = round(realized + unrealized, 2) if realized is not None and unrealized is not None else None
        details = []
        for row in reversed(rows):
            eligible_row = row in eligible
            details.append({
                'time_ny': row.get('hora_ny'), 'ticker': row.get('ticker'),
                'side': row.get('position_side') or row.get('lado'),
                'quantity': number(row.get('cantidad')), 'entry': number(row.get('entrada')),
                'exit': number(row.get('salida')), 'recorded_pnl': number(row.get('pnl_usd')),
                'included_pnl': number(row.get('pnl_usd')) if eligible_row else None,
                'included': eligible_row, 'reason': row.get('motivo'),
                'exclusion_kind': row.get('performance_exclusion_kind', 'UNVERIFIED'),
                'exclusion_reason': row.get('performance_exclusion_reason'),
            })
        manual = [r for r in intentional if r.get('performance_exclusion_kind') == 'VERIFIED_MANUAL_EXIT']
        modes[mode] = {
            'mode': mode, 'source': 'PAPER_SIMULATION' if mode == 'PAPER' else 'LOCAL_RECORDED_FILLS_GROSS',
            'is_broker_account_pnl': False,
            'closed_pnl': realized, 'known_closed_pnl': known_closed,
            'open_pnl': unrealized, 'known_open_pnl': known_open, 'total_pnl': net,
            'closed_count': len(eligible), 'open_count': len(opened),
            'intentionally_excluded_count': len(intentional), 'unverified_closed_count': len(unknown),
            'unverified_open_count': len(unknown_open),
            'verified_manual_closed_pnl': round(sum(float(r['pnl_usd']) for r in manual), 2),
            'verified_manual_closed_count': len(manual),
            'maintenance_excluded_count': sum(r.get('performance_exclusion_kind') == 'MAINTENANCE' for r in intentional),
            'tickers': [{'ticker': r['ticker'], **r['mode_breakdown'][mode]} for r in performance
                if any(r['mode_breakdown'][mode][key] for key in ('closed_trades','excluded_closed_trades','open_positions'))],
            'closes': details,
        }
    return {'date_ny': session_date, 'as_of_utc': datetime.now(timezone.utc).isoformat(),
            'modes': modes, 'is_broker_account_pnl': False,
            'note': 'PAPER es simulación. REAL usa cierres GALA verificados, sin comisiones. '
                    'Los cierres manuales verificados se muestran aparte; el flotante REAL y el P/L de cuenta requieren consulta Schwab.'}
