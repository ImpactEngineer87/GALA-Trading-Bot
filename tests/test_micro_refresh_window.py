"""Incremental reads retain indicator warmup and periodically correct old bars."""
from datetime import datetime,timedelta,timezone
import threading
import unittest
from unittest.mock import Mock,patch
import pandas as pd
from agents.contracts import DatosMultiTemporal
from api.schwab_client import SchwabClient
from engine.bot_engine import ChocoletraBot


class MicroRefreshWindowTests(unittest.TestCase):
    def setUp(self):
        self.frame=pd.DataFrame({k:1. for k in ['open','high','low','close','volume']},index=pd.date_range('2026-11-10T15:00Z',periods=120,freq='min'))
        b=self.bot=ChocoletraBot.__new__(ChocoletraBot)
        b._mtf_cache={'BA':(1000,DatosMultiTemporal('BA',{'1m':self.frame.copy()}),1000)}
        b._full_micro_refresh_at={'BA':800};b._mtf_ttl_macro=300;b._mtf_ttl=0
        b.schwab=Mock();b.schwab._asegurar_token.return_value=True;b._emit_log=Mock()

    def test_delta_retains_full_history_and_full_refresh_applies_older_corrections(self):
        delta=self.frame.tail(2).copy();delta.iloc[-1,delta.columns.get_loc('close')]=2.
        self.bot.schwab.obtener_1m_reciente.return_value=delta
        with patch('engine.bot_engine.time.time',return_value=1001):out=self.bot._obtener_mtf('BA')
        self.assertEqual(len(out.tf('1m')),120);self.assertEqual(out.tf('1m')['close'].iloc[-1],2.)
        self.bot.schwab.obtener_1m_reciente.assert_called_once_with('BA',since=self.frame.index[-1]-pd.Timedelta(minutes=10))
        corrected=self.frame.copy();corrected.iloc[0,corrected.columns.get_loc('close')]=9.
        self.bot.schwab.obtener_1m_reciente.reset_mock();self.bot.schwab.obtener_1m_reciente.return_value=corrected
        with patch('engine.bot_engine.time.time',return_value=1102):out=self.bot._obtener_mtf('BA')
        self.bot.schwab.obtener_1m_reciente.assert_called_once_with('BA')
        self.assertEqual(out.tf('1m')['close'].iloc[0],9.)
        self.assertEqual(self.bot._full_micro_refresh_at['BA'],1102)

    def test_failed_full_refresh_cannot_mark_micro_or_correction_window_fresh(self):
        self.bot.schwab.obtener_1m_reciente.return_value=None
        with patch('engine.bot_engine.time.time',return_value=1102):self.bot._obtener_mtf('BA')
        self.assertEqual(self.bot._full_micro_refresh_at['BA'],800)
        self.assertEqual(self.bot._mtf_cache['BA'][2],1000)

    def test_http_window_preserves_native_one_minute_source_and_default_full_read(self):
        client=SchwabClient.__new__(SchwabClient);client.include_extended_hours_data=False
        client._pedir_pricehistory=Mock(return_value=self.frame);client._fetch_minutos_chunked=Mock()
        since=datetime.now(timezone.utc)-timedelta(minutes=10)
        client.obtener_1m_reciente('BA',since=since)
        params=client._pedir_pricehistory.call_args.args[0]
        self.assertEqual(params['symbol'],'BA');self.assertEqual(params['frequency'],1)
        self.assertEqual(params['frequencyType'],'minute');self.assertFalse(params['needExtendedHoursData'])
        self.assertLessEqual(abs(params['startDate']-int(since.timestamp()*1000)),1)
        client._fetch_minutos_chunked.assert_not_called()
        client.obtener_1m_reciente('BA');client._fetch_minutos_chunked.assert_called_once_with('BA',1,2)

    def test_stopped_engine_does_not_fetch_or_publish_queued_analysis(self):
        self.bot._activo=False;self.bot._obtener_mtf=Mock()
        self.bot._procesar_ticker('BA');self.bot._obtener_mtf.assert_not_called()
        self.bot._activo=True;self.bot._is_paper_ticker=Mock(return_value=False)
        def finish_fetch(ticker):self.bot._activo=False;return object()
        self.bot._obtener_mtf=Mock(side_effect=finish_fetch);self.bot._datos_micro_confirmados=Mock()
        self.bot._procesar_ticker('BA');self.bot._datos_micro_confirmados.assert_not_called()


if __name__=='__main__':unittest.main()
