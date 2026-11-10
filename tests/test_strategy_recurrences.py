"""Preserve master-strategy recurrence and omitted-signal priority semantics."""
import unittest
import pandas as pd
from core.strategy import GALA10Strategy


class StrategyRecurrenceTests(unittest.TestCase):
    def test_cross_extremes_and_direction_remain_until_the_next_matching_cross(self):
        frame=pd.DataFrame({'high':[10,11,12,13,14,15], 'low':[5,4,3,2,1,0],
            'bull_cross':[False,True,False,False,False,True],
            'bear_cross':[False,False,False,True,False,False]})
        result=GALA10Strategy({})._calcular_memoria_cruce(frame)
        self.assertEqual(result['cross_high'].tolist(),[10,11,11,11,11,15])
        self.assertEqual(result['cross_low'].tolist(),[5,5,5,2,2,2])
        self.assertEqual(result['cross_bars'].tolist(),[0,0,1,0,1,0])
        self.assertEqual(result['direccion_activa'].tolist(),[0,1,1,-1,-1,1])

    def test_suppressed_raw_signals_still_reset_cooldown_and_priority_is_preserved(self):
        frame=pd.DataFrame({'buy_signal_raw':[True,True,False,True,False,False,True],
            'sell_signal_raw':[True,False,False,True,False,False,False],
            'omitida_buy_rango':[False,True,False,False,False,False,False],
            'omitida_sell_rango':[False]*7,'omitida_buy_tendencia':[False,True,True,False,False,False,False],
            'omitida_sell_tendencia':[False]*7})
        result=GALA10Strategy({'strategy':{'cooldown_bars':2}})._aplicar_cooldown(frame)
        self.assertEqual(result['barras_desde_buy'].tolist(),[100,1,1,2,1,2,3])
        self.assertEqual(result['barras_desde_sell'].tolist(),[100,1,2,3,1,2,3])
        self.assertEqual([v if pd.notna(v) else None for v in result['señal']],
                         ['BUY','OM-R','OM-T','SELL',None,None,'BUY'])
        self.assertEqual(result['buy'].tolist(),[True,False,False,False,False,False,True])
        self.assertTrue(result['sell'].iloc[0])


if __name__=='__main__':unittest.main()
