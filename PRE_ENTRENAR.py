# =============================================================
# Lanzador de PRE-ENTRENAMIENTO histórico de los 3 Agentes SMC.
# Reproduce el historial de cada acción y entrena el meta-modelo
# ANTES de operar en vivo, para que el bot NO empiece de cero.
#
# Uso:   python PRE_ENTRENAR.py
#
# La hora de cada vela histórica se pasa explícitamente al Agente 3.
# Por tanto se conserva el MISMO filtro horario configurado para producción:
# las velas extended alimentan Gala10, pero solo las señales dentro de la
# ventana de ejecución pueden convertirse en ejemplos de operaciones.
# =============================================================
import yaml
from agents.backtest_trainer import PreEntrenadorHistorico

c = yaml.safe_load(open('config.yaml', encoding='utf-8'))
DEF = ['BA','AAPL','TSLA','NVDA','AMZN','META','AMD','RCL','UNH','MSFT',
       'GOOGL','NFLX','JPM','BAC','DIS','INTC','QCOM','PYPL','SBUX','CAT']
tickers = c.get('market', {}).get('tickers') or DEF
PreEntrenadorHistorico(c).pre_entrenar(tickers)
