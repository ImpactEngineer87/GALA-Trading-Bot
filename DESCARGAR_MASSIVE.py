"""
=============================================================
DESCARGAR_MASSIVE.py  —  Descarga histórico de 2 años (Massive) a CSV
=============================================================
Descarga ~2 años de velas de 1 minuto de cada acción desde Massive
(ex Polygon.io) y las guarda en data/historico/. La sesión se controla con
market.include_extended_hours_data; Gala10/TOS requiere extended=true para
reproducir su estado acumulado, aunque las órdenes permanezcan limitadas a RTH.

Luego corré PRE_ENTRENAR.py: los modelos se entrenan con esos 2 años.

REQUISITOS:
  - En el .env:  MASSIVE_API_KEY=tu_clave_de_massive
  - En config.yaml: market.tickers con la lista de acciones.

USO:
  python DESCARGAR_MASSIVE.py
=============================================================
"""
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import yaml
from agents.massive_downloader import descargar_todos

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    tickers = cfg.get("market", {}).get("tickers", []) or \
        ["BA", "AAPL", "TSLA", "NVDA", "AMZN", "META", "AMD", "RCL", "UNH"]
    anios = float(cfg.get("ai", {}).get("historico_anios", 2))

    print("\n" + "=" * 60)
    print("  DESCARGA DE HISTÓRICO (MASSIVE) → data/historico/")
    print(f"  Tickers: {len(tickers)} | años: {anios}")
    print("=" * 60 + "\n")

    res = descargar_todos(tickers, cfg, anios=anios)
    if not res:
        print("\nNo se descargó nada. Revisá MASSIVE_API_KEY en el .env.")
        sys.exit(1)
    print("\nListo. Ahora corré:  python PRE_ENTRENAR.py")
