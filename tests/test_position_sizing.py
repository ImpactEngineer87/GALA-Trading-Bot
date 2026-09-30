import unittest

from core.risk_manager import GestorDeRiesgo


class PositionSizingTests(unittest.TestCase):
    def test_fixed_mode_uses_exact_manual_quantity(self):
        manager = GestorDeRiesgo({
            "risk": {
                "paper_trading": False,
                "sizing_mode": "fixed",
                "fixed_shares": 50,
                "max_acciones_por_orden": 0,
            },
            "salida_parcial": {"activo": True},
        })
        order = manager.calcular_orden(
            ticker="AAPL", tipo="BUY", precio_actual=200.0, atr=1.0,
            capital_disponible=10000.0,
        )
        self.assertEqual(order.cantidad, 50)
        self.assertEqual(order.cantidad_parcial + order.cantidad_resto, 50)

    def test_hard_cap_still_overrides_manual_quantity_for_live_validation(self):
        manager = GestorDeRiesgo({
            "risk": {
                "paper_trading": False,
                "sizing_mode": "fixed",
                "fixed_shares": 50,
                "max_acciones_por_orden": 1,
            },
            "salida_parcial": {"activo": True},
        })
        order = manager.calcular_orden(
            ticker="AAPL", tipo="BUY", precio_actual=200.0, atr=1.0,
            capital_disponible=10000.0,
        )
        self.assertEqual(order.cantidad, 1)
        self.assertFalse(order.salida_parcial_activa)

    def test_atr_mode_uses_configured_risk_percentage(self):
        manager = GestorDeRiesgo({
            "risk": {
                "paper_trading": False,
                "sizing_mode": "atr",
                "risk_per_trade_pct": 2.0,
                "stop_loss_factor": 2.0,
            },
            "salida_parcial": {"activo": True},
        })
        order = manager.calcular_orden(
            ticker="AAPL", tipo="BUY", precio_actual=200.0, atr=1.0,
            capital_disponible=10000.0,
        )
        self.assertEqual(order.cantidad, 100)  # $200 risk / $2 per share

    def test_independent_sl_tp_factors_and_fixed_20_share_risk(self):
        manager = GestorDeRiesgo({
            "risk": {
                "paper_trading": False,
                "sizing_mode": "fixed",
                "fixed_shares": 20,
                "max_acciones_por_orden": 20,
                "stop_loss_factor": 3.5,
                "take_profit_factor": 4.5,
            },
            "salida_parcial": {
                "activo": True,
                "pct_primer_objetivo": 0.70,
                "rb_primer_objetivo": 1.0,
            },
        })
        order = manager.calcular_orden(
            ticker="AAPL", tipo="BUY", precio_actual=200.0, atr=2.0,
            capital_disponible=10000.0,
        )

        self.assertEqual(order.cantidad, 20)
        self.assertEqual(order.stop_loss, 193.0)       # 200 - 3.5*2
        self.assertEqual(order.take_profit, 209.0)     # 200 + 4.5*2
        self.assertEqual(order.take_profit_parcial, 207.0)
        self.assertEqual(order.riesgo_usd, 140.0)      # 20 * $7
        self.assertEqual(order.stop_loss_atr_factor, 3.5)
        self.assertEqual(order.take_profit_atr_factor, 4.5)
        self.assertEqual(order.cantidad_parcial, 14)
        self.assertEqual(order.cantidad_resto, 6)


if __name__ == "__main__":
    unittest.main()
