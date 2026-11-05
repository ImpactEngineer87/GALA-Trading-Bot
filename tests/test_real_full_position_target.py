"""REAL full exits honor TP without requiring a partial fill or real orders."""
import threading
import unittest
from unittest.mock import Mock,patch
from engine.bot_engine import ChocoletraBot


class RealFullPositionTargetTests(unittest.TestCase):
    def setUp(self):
        self.network=self.enterContext(patch('requests.sessions.Session.request',
            side_effect=AssertionError('Full-position target tests must not contact broker')))

    def tearDown(self):self.network.assert_not_called()

    def bot(self,side='SELL',*,partial=False,tp1=False):
        bot=ChocoletraBot.__new__(ChocoletraBot);bot._activo=True
        bot.account_number='offline-account';bot._lock=threading.RLock()
        pos={'tipo':side,'sl_order_id':'confirmed-stop','sl_reconcile_pending':False,
             'salida_parcial':partial,'tp1_hit':tp1,'tp1_order_id':'partial' if partial else '',
             'normal_exit_progress':{},'tp_final':170.04}
        bot._posiciones={'BA':pos};bot._asegurar_token_trading=Mock(return_value=True)
        bot.ejecutor_hibrido=Mock();bot.ejecutor_hibrido._estado_orden.return_value='WORKING'
        bot._cerrar_posicion=Mock(return_value=True)
        return bot,pos

    def test_full_short_and_long_close_at_target_without_a_partial_fill(self):
        for side,price in [('SELL',169.895),('BUY',170.20),('SELL',170.04),('BUY',170.04)]:
            with self.subTest(side=side,price=price):
                bot,pos=self.bot(side)
                bot._gestionar_real('BA',pos,price)
                bot._cerrar_posicion.assert_called_once_with('BA',170.04,motivo='TP final')
                self.assertEqual(pos['sl_order_id'],'confirmed-stop')
                bot.ejecutor_hibrido.colocar_salida.assert_not_called()

    def test_full_exit_before_target_does_not_close(self):
        for side,price in [('SELL',170.20),('BUY',169.895)]:
            bot,pos=self.bot(side);bot._gestionar_real('BA',pos,price)
            bot._cerrar_posicion.assert_not_called()

    def test_partial_plan_still_waits_for_tp1_then_honors_final_target(self):
        bot,pos=self.bot(partial=True);bot._gestionar_real('BA',pos,169.895)
        bot._cerrar_posicion.assert_not_called()
        pos['tp1_hit']=True;bot._gestionar_real('BA',pos,169.895)
        bot._cerrar_posicion.assert_called_once_with('BA',170.04,motivo='TP final')

    def test_stopped_engine_does_not_manage_a_target(self):
        bot,pos=self.bot();bot._activo=False
        bot._gestionar_real('BA',pos,169.895);bot._cerrar_posicion.assert_not_called()

