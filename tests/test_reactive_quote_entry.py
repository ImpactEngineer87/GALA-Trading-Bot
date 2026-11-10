"""Executable LIMIT pricing preserves the pre-existing slippage budget."""
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from api.hybrid_order_executor import EjecutorHibridoDeOrdenes


class ReactiveQuoteEntryTests(unittest.TestCase):
    def setUp(self):
        folder=self.enterContext(tempfile.TemporaryDirectory())
        self.x=EjecutorHibridoDeOrdenes.__new__(EjecutorHibridoDeOrdenes)
        self.x.paper_trading=False;self.x.max_slippage_pct=.2
        self.x.execution_v2_config={'enabled':True,'pending_path':folder+'/pending.json','history_path':folder+'/events.jsonl'}
        self.order=SimpleNamespace(tipo='BUY',precio_limite=172.09,ticker='BA',cantidad=5)

    def price(self,bid,ask):
        return self.x._submitted_limit_price(self.order,'LIMIT',quote={'bid':bid,'ask':ask},reactive_entry=True)

    def test_ba_incident_uses_fresh_ask_within_existing_point_two_percent_cap(self):
        self.assertEqual(self.price(172.07,172.40),172.40)
        self.assertLessEqual(self.price(172.07,173.),172.09*1.002)
        self.assertEqual(self.price(172.07,173.),172.43)

    def test_short_uses_bid_and_cent_rounding_cannot_cross_adverse_cap(self):
        self.order.tipo='SELL';self.order.precio_limite=100.0123
        self.assertEqual(self.price(99.901,100.),99.90)
        result=self.price(99.70,99.80)
        self.assertEqual(result,99.82);self.assertGreaterEqual(result,100.0123*.998)

    def test_explicit_tighter_dollar_cap_and_explicit_zero_are_preserved(self):
        self.x.execution_v2_config['entry_limit_tolerance_usd']=.05
        self.assertEqual(self.price(172.07,172.4),172.14)
        self.x.execution_v2_config['entry_limit_tolerance_usd']=0
        self.assertEqual(self.price(172.07,172.4),172.09)

    def test_paper_and_passive_entries_do_not_gain_quote_chasing(self):
        self.assertEqual(self.x._submitted_limit_price(self.order,'LIMIT',quote={'bid':172.07,'ask':172.4}),172.09)
        self.x.paper_trading=True;self.assertEqual(self.price(172.07,172.4),172.09)

    def test_actual_submission_is_limit_and_stale_quotes_use_reference(self):
        response=Mock();response.headers={'Location':'https://broker/orders/test-order'}
        self.x._submit_order_request=Mock(return_value=response)
        self.x._audit=Mock();self.x.request_timeout_s=5;self.x.ack_timeout_s=1
        self.x.esperar_estado_orden=Mock(return_value={'confirmado':True,'latency_ms':1,'broker_status':'WORKING'})
        self.x._quote_provider=lambda ticker:{'bid':172.07,'ask':172.4,'quote_time':time.time()}
        self.x.TRADER_URL='https://broker'
        for age,expected in [(0,172.4),(8,172.09)]:
            self.x._quote_provider=lambda ticker:{'bid':172.07,'ask':172.4,'quote_time':time.time()-age}
            self.x._enviar_orden_real(self.order,'LIMIT','test-account','reactive',reactive_entry=True)
            body=self.x._submit_order_request.call_args.kwargs['json']
            self.assertEqual(body['orderType'],'LIMIT');self.assertEqual(float(body['price']),expected)
            self.assertEqual(body['orderLegCollection'][0]['quantity'],5)
            self.assertEqual(body['orderLegCollection'][0]['instruction'],'BUY')

    def test_engine_entry_preserves_reactive_intent_through_limit_override(self):
        self.x._normalizar_cantidad_final=Mock(return_value=self.order)
        self.x._enviar_orden_real=Mock(return_value={'status':'OK'})
        with patch('api.hybrid_order_executor.calcular_orden_hibrida',return_value=(self.order,'MARKET','reactive')):
            self.x.enviar_entrada(self.order,SimpleNamespace(es_entrada_mercado=True),'test-account')
        self.assertEqual(self.x._enviar_orden_real.call_args.args[1],'LIMIT')
        self.assertTrue(self.x._enviar_orden_real.call_args.kwargs['reactive_entry'])


if __name__=='__main__':unittest.main()
