"""Delayed fills recover on the same engine; no HTTP or real orders."""
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from core.runtime_store import JsonState
from tests import test_real_accounting as fixtures


class ManualCloseRetryTests(unittest.TestCase):
    setUp=fixtures.RealCloseAccountingTests.setUp
    engine=fixtures.RealCloseAccountingTests.engine
    last_close=fixtures.RealCloseAccountingTests.last_close
    def test_delayed_fill_is_retained_then_recovers_without_a_restart(self):
        bot=self.engine();bot._activo=True
        bot.manual_close_pending=JsonState(self.root/'pending.json')
        bot._ahora_et=lambda:datetime.now(ZoneInfo('America/New_York'))
        bot._cantidad_real_broker.return_value=0
        bot._recuperar_cierre_manual_verificado=Mock(return_value={})
        self.assertFalse(bot._cerrar_posicion_impl('NKE',99,'manual'))
        self.assertIn('NKE',bot._posiciones)
        self.assertFalse((self.root/'history.csv').exists())
        self.assertTrue(bot._real_accounting_status()['manual_close_retrying'])
        self.assertFalse(bot.real_accounting_guard.snapshot()['blocked'])
        self.assertEqual(bot.daily_loss_guard.realized_pnl(False),0)
        bot._recuperar_cierre_manual_verificado.return_value={
            'order_id':'manual','avg_price':37.595,'quantity':10,'tag':'','order_type':'LIMIT'}
        self.assertTrue(bot._cerrar_posicion_impl('NKE',99,'manual'))
        self.assertTrue(bot._activo)
        self.assertNotIn('NKE',bot._posiciones)
        self.assertFalse(bot._real_accounting_status()['entry_blocked'])
        self.assertFalse(bot.manual_close_pending.get('pending'))
        self.assertAlmostEqual(bot.daily_loss_guard.realized_pnl(False),-.65)
        bot.ejecutor_hibrido.liquidar_posicion.assert_not_called()
        self.assertFalse(bot._cerrar_posicion_impl('NKE',99,'repeat'))
        self.assertAlmostEqual(bot.daily_loss_guard.realized_pnl(False),-.65)

    def test_working_stop_does_not_hide_a_manual_flat_position(self):
        bot=self.engine();bot._activo=True
        bot.manual_close_pending=JsonState(self.root/'pending.json')
        bot._manual_broker_quantity=Mock(return_value=0)
        bot._cantidad_real_broker.return_value=0
        bot._cerrar_posicion=Mock()
        pos=bot._posiciones['NKE'];pos['sl_order_id']='working-stop'
        bot.ejecutor_hibrido._estado_orden.return_value='WORKING'
        bot._gestionar_real('NKE',pos,37.5)
        bot._cerrar_posicion.assert_called_once_with('NKE',37.5,
            motivo='reconciliacion automatica: broker plano')
        bot.ejecutor_hibrido._estado_orden.assert_not_called()

    def test_cached_flat_before_a_new_entry_does_not_cancel_its_protection(self):
        bot=self.engine();bot._activo=True
        bot.manual_close_pending=JsonState(self.root/'pending.json')
        bot._manual_broker_quantity=Mock(return_value=0)
        bot._cantidad_real_broker.return_value=10
        bot._cerrar_posicion=Mock()
        pos=bot._posiciones['NKE'];pos.update(sl_order_id='working',sl_reconcile_pending=False,
            salida_parcial=False,tp_final=0,sl_actual=37)
        bot.ejecutor_hibrido._estado_orden.return_value='WORKING'
        bot._gestionar_real('NKE',pos,37.5)
        bot._cerrar_posicion.assert_not_called()

    def test_position_snapshot_is_shared_and_unknown_is_never_flat(self):
        bot=self.engine();bot._manual_observation_lock=threading.Lock()
        bot._manual_positions_cache={'until':0,'positions':None}
        bot.schwab=Mock();bot.schwab.obtener_posiciones.return_value=[]
        self.assertEqual(bot._manual_broker_quantity('NKE','BUY'),0)
        self.assertEqual(bot._manual_broker_quantity('META','BUY'),0)
        bot.schwab.obtener_posiciones.assert_called_once()
        bot._manual_positions_cache['until']=0
        bot.schwab.obtener_posiciones.return_value=None
        self.assertIsNone(bot._manual_broker_quantity('NKE','BUY'))
