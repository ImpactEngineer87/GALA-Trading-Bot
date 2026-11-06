"""Slow historical GETs cannot occupy a current-candle analysis slot."""
import threading
import time
import unittest
from unittest.mock import Mock,patch
import pandas as pd
from agents.contracts import DatosMultiTemporal
from core.macro_refresh import MacroRefresh
from engine.bot_engine import ChocoletraBot
from api.schwab_client import SchwabClient


class MacroRefreshTests(unittest.TestCase):
    def setUp(self):
        frame=pd.DataFrame({key:1. for key in ['open','high','low','close','volume']},
            index=pd.date_range('2026-11-06T15:00:00Z',periods=120,freq='min'))
        self.data=DatosMultiTemporal('TSLA',{'1m':frame})
        self.bot=ChocoletraBot.__new__(ChocoletraBot)
        self.bot._mtf_cache={'TSLA':(800,self.data,1000)}
        self.bot._mtf_ttl_macro,self.bot._mtf_ttl=300,0
        self.bot.config={};self.bot._emit_log=Mock()
        self.bot.schwab=Mock()
        self.bot.schwab._asegurar_token.return_value=True
        fresh=frame.copy();fresh.loc[frame.index[-1],'close']=2.
        self.bot.schwab.obtener_1m_reciente.return_value=fresh

    def test_pending_full_history_still_reads_and_returns_fresh_micro_candles(self):
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        def fetch(symbol):
            entered.set();release.wait(3);finished.set();return self.data
        refresher=MacroRefresh(fetch);self.bot._macro_refresh=refresher
        self.addCleanup(refresher.close);self.addCleanup(release.set)
        with patch('engine.bot_engine.time.time',return_value=1001):
            result=self.bot._obtener_mtf('TSLA')
        self.assertTrue(entered.wait(2));self.assertFalse(finished.is_set())
        self.assertEqual(result.tf('1m').iloc[-1]['close'],2.)
        self.bot.schwab.obtener_multitemporal.assert_not_called()
        self.bot.schwab.obtener_1m_reciente.assert_called_once_with('TSLA')
        self.assertFalse(refresher.request('TSLA'))

    def test_old_background_micro_cannot_replace_fresher_main_thread_prices(self):
        older=DatosMultiTemporal('TSLA',{'1m':self.data.tf('1m').copy()})
        self.data.velas['1m'].iloc[-1,self.data.velas['1m'].columns.get_loc('close')]=3.
        self.bot._macro_refresh=Mock()
        self.bot._macro_refresh.completed.return_value=(older,1000)
        self.bot._mtf_ttl=100
        with patch('engine.bot_engine.time.time',return_value=1001):
            result=self.bot._obtener_mtf('TSLA')
        self.assertEqual(result.tf('1m').iloc[-1]['close'],3.)

    def test_history_failure_is_reported_while_micro_polling_continues(self):
        self.bot._macro_refresh=Mock()
        self.bot._macro_refresh.completed.side_effect=RuntimeError('unavailable')
        with patch('engine.bot_engine.time.time',return_value=1001):
            result=self.bot._obtener_mtf('TSLA')
        self.assertEqual(result.tf('1m').iloc[-1]['close'],2.)
        self.bot._emit_log.assert_called_once()

    def test_history_is_not_served_indefinitely_after_refresh_failures(self):
        self.bot._macro_refresh=Mock();self.bot._macro_refresh.completed.return_value=None
        self.bot.schwab.obtener_multitemporal.return_value=self.data
        with patch('engine.bot_engine.time.time',return_value=1400):
            self.bot._obtener_mtf('TSLA')
        self.bot.schwab.obtener_multitemporal.assert_called_once_with('TSLA',{})

    def test_closed_refresher_cannot_schedule_or_install_results(self):
        refresher=MacroRefresh(Mock(return_value=self.data));refresher.close()
        self.assertFalse(refresher.request('TSLA'))
        self.assertIsNone(refresher.completed('TSLA'))

    def test_native_context_refresh_retains_history_and_uses_only_recent_intraday_pages(self):
        client=SchwabClient.__new__(SchwabClient)
        original=self.data.tf('1m')
        previous=DatosMultiTemporal('TSLA',{tf:original.copy() for tf in ['1m','5m','15m','30m']})
        fresh=original.iloc[-45:].copy();fresh.iloc[-1,fresh.columns.get_loc('close')]=7.
        client.obtener_historico=Mock(return_value=fresh)
        client._fetch_minutos_chunked=Mock(return_value=fresh)
        client.obtener_multitemporal=Mock()
        result=client.refrescar_contexto_multitemporal('TSLA',previous,{})
        self.assertEqual(len(result.tf('30m')),120)
        self.assertEqual(result.tf('30m').iloc[-1]['close'],7.)
        self.assertEqual(previous.tf('30m').iloc[-1]['close'],1.)
        pd.testing.assert_frame_equal(previous.tf('1m'),result.tf('1m'))
        self.assertEqual(client._fetch_minutos_chunked.call_args_list,
            [unittest.mock.call('TSLA',f,2) for f in [30,15,5]])
        client.obtener_multitemporal.assert_not_called()

    def test_missing_refresh_data_cannot_reset_context_age(self):
        client=SchwabClient.__new__(SchwabClient)
        previous=DatosMultiTemporal('TSLA',{'1m':self.data.tf('1m'),'30m':self.data.tf('1m')})
        client.obtener_historico=Mock(return_value=None)
        with self.assertRaises(RuntimeError):
            client.refrescar_contexto_multitemporal('TSLA',previous,{})


if __name__=='__main__':unittest.main()
