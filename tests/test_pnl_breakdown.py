"""Reconstruct the client amount without mixing modes or inventing unknown P/L."""
import unittest
from datetime import datetime,timezone
from unittest.mock import Mock,patch
from core.pnl_breakdown import build_pnl_breakdown
from core.ticker_performance import build_ticker_performance,summarize_modes
from core.dashboard_evidence import annotate_history


class PnlBreakdownTests(unittest.TestCase):
    def close(self,ticker='AMD',pnl=-397.05,mode='PAPER',**kw):
        return dict(evento='POSITION_CLOSE',fecha='2026-11-10',ticker=ticker,modo=mode,
            pnl_usd=pnl,performance_verified=True,position_id='p',execution_id='e',**kw)

    def build(self,rows,positions=()):return build_pnl_breakdown(rows,positions,'2026-11-10')

    def test_screenshot_arithmetic_and_each_contributing_trade_is_visible(self):
        rows=[self.close(),self.close('META',7.4,'REAL')]
        pos=[dict(ticker='AMD',mode='PAPER',status='OPEN',floating_pnl=25.09,valuation_available=True)]
        result=self.build(rows,pos);p=result['modes']['PAPER'];real=result['modes']['REAL']
        self.assertEqual(p['closed_pnl'],-397.05);self.assertEqual(p['open_pnl'],25.09)
        self.assertEqual(p['total_pnl'],-371.96);self.assertEqual(real['closed_pnl'],7.4)
        self.assertEqual(p['closes'][0]['included_pnl'],-397.05)
        self.assertFalse(result['is_broker_account_pnl'])

    def test_intentional_maintenance_does_not_make_known_paper_totals_unavailable(self):
        row=self.close();excluded=self.close('MSFT',-99)
        excluded.update(performance_verified=False,performance_intentionally_excluded=True,performance_exclusion_kind='MAINTENANCE')
        p=self.build([row,excluded])['modes']['PAPER']
        self.assertEqual(p['closed_pnl'],-397.05);self.assertEqual(p['maintenance_excluded_count'],1)
        self.assertEqual(p['unverified_closed_count'],0)
        perf=build_ticker_performance(['AMD','MSFT'],history_rows=[row,excluded],session_date='2026-11-10')
        self.assertEqual(summarize_modes(perf)['PAPER']['realized_pnl'],-397.05)

    def test_unknown_close_or_stale_open_stays_unknown_not_zero(self):
        row=self.close();bad=self.close('UNH',-9);bad['performance_verified']=False
        self.assertIsNone(self.build([row,bad])['modes']['PAPER']['closed_pnl'])
        p=self.build([row],[dict(ticker='AMD',mode='PAPER',status='OPEN',floating_pnl=None)])['modes']['PAPER']
        self.assertIsNone(p['total_pnl']);self.assertIsNone(p['open_pnl'])
        self.assertEqual(p['known_closed_pnl'],-397.05)

    def test_known_manual_close_is_separate_while_unverified_real_still_blocks_total(self):
        normal=self.close('BA',5,'REAL');manual=self.close('TSLA',-2,'REAL')
        manual.update(performance_verified=False,performance_intentionally_excluded=True,performance_exclusion_kind='VERIFIED_MANUAL_EXIT')
        real=self.build([normal,manual])['modes']['REAL']
        self.assertEqual(real['closed_pnl'],5);self.assertEqual(real['verified_manual_closed_pnl'],-2)
        self.assertEqual(real['unverified_closed_count'],0)
        unknown=self.close('NVDA',-3,'REAL');unknown['performance_verified']=False
        self.assertIsNone(self.build([normal,manual,unknown])['modes']['REAL']['closed_pnl'])

    def test_empty_real_tickers_do_not_erase_confirmed_closes_in_mode_summary(self):
        row=self.close('BA',5,'REAL')
        perf=build_ticker_performance(['BA','AMD'],history_rows=[row],session_date='2026-11-10')
        self.assertEqual(summarize_modes(perf)['REAL']['realized_pnl'],5)

    def test_mode_totals_round_after_summing_original_subcent_values(self):
        rows=[self.close('BA',.005,'REAL'),self.close('META',.005,'REAL')]
        perf=build_ticker_performance(['BA','META'],history_rows=rows,session_date='2026-11-10')
        self.assertEqual(summarize_modes(perf)['REAL']['realized_pnl'],.01)
        self.assertEqual(self.build(rows)['modes']['REAL']['closed_pnl'],.01)

    def test_manual_exclusion_requires_complete_verified_fills_and_risk(self):
        row=dict(evento='POSITION_CLOSE',fecha='2026-11-10',ticker='TSLA',modo='REAL',
            position_id='p',execution_id='e',cantidad=3,entrada=100,salida=101,pnl_usd=3,
            position_side='LONG',parent_order_id='entry',order_id='exit')
        base={k:row[k] for k in ['modo','ticker','position_id','execution_id','position_side']}
        entry={**base,'evento':'ENTRY_FILLED','order_id':'entry','estado':'FILLED',
            'cantidad':3,'precio':100,'detalle':{'broker_price_verified':True}}
        close={**base,'evento':'POSITION_CLOSED','order_id':'exit','estado':'FILLED',
            'cantidad':3,'precio':101,'pnl_usd':3,'detalle':{'broker_confirmed':True,
            'fill_price_verified':True,'salida_real':101,'external_manual_exit':True,'manual_risk_verified':True}}
        result=annotate_history([row],[entry,close])[0]
        self.assertTrue(result['performance_intentionally_excluded']);self.assertTrue(result['accounting_verified'])
        self.assertEqual(result['performance_exclusion_kind'],'VERIFIED_MANUAL_EXIT')
        close['detalle']['manual_risk_verified']=False
        self.assertFalse(annotate_history([row],[entry,close])[0]['performance_intentionally_excluded'])
        close['detalle']['manual_risk_verified']=True;entry['detalle']['broker_price_verified']=False
        self.assertFalse(annotate_history([row],[entry,close])[0]['performance_intentionally_excluded'])

    def test_api_explains_simulation_without_starting_or_querying_broker(self):
        import server
        engine=Mock();rows=[self.close()]
        positions=[dict(ticker='AMD',mode='PAPER',status='OPEN',floating_pnl=25.09)]
        with patch.object(server,'_ENGINE',engine),patch.object(server,'_dashboard_history_rows',return_value=rows),patch.object(
                server,'_engine_positions_snapshot',return_value=positions),patch.object(
                server,'_ahora_ny_datetime',return_value=datetime(2026,11,10,20,tzinfo=timezone.utc)),server.app.test_client() as client:
            with client.session_transaction() as session:session['logged_in']=True
            response=client.get('/api/pnl-breakdown')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['modes']['PAPER']['total_pnl'],-371.96)
            self.assertFalse(response.json['is_broker_account_pnl'])
        self.assertEqual(engine.mock_calls,[])

    def test_pending_and_other_session_rows_are_not_open_or_today_pnl(self):
        row=self.close();row['fecha']='2026-11-09'
        p=self.build([row],[dict(ticker='AMD',mode='PAPER',status='PENDING',floating_pnl=99)])['modes']['PAPER']
        self.assertEqual(p['closed_count'],0);self.assertEqual(p['open_count'],0);self.assertEqual(p['total_pnl'],0)


if __name__=='__main__':unittest.main()
