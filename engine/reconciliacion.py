"""
=============================================================
MÓDULO: engine/reconciliacion.py  (A2 del PLAN_DE_CONSTRUCCION)
PROYECTO: GALA6 Chocoletra Bot
=============================================================
DESCRIPCIÓN:
    Lógica PURA de reconciliación entre el estado interno del bot y la posición
    REAL de la cuenta de Schwab. Sin efectos secundarios ni llamadas de red →
    testeable de forma aislada.

    Resuelve la "independencia operativa manual" que pide el cliente: si se
    compra/vende/cierra a mano en ThinkOrSwim o Schwab, el motor detecta la
    diferencia y decide qué hacer, sin descuadrarse ni fallar.

    Decisiones:
      - adoptar : hay posición REAL que el bot no tenía registrada (la intervino
                  un humano) → el motor debe empezar a gestionarla o dejarla estar.
      - soltar  : el bot creía tener una posición que en la cuenta YA NO existe
                  (la cerró un humano o saltó un stop) → soltar el estado interno.
      - ajustar : coinciden el ticker pero difiere dirección/cantidad → alinear.
"""
from __future__ import annotations


def reconciliar(interno: dict, real: dict) -> dict:
    """
    Compara estado interno vs. real y devuelve las acciones necesarias.

    interno: {ticker: {'tipo': 'BUY'|'SELL', 'cantidad': int, ...}}
             (posiciones que el bot cree tener; 'reservado' se ignora)
    real:    {ticker: {'cantidad': int, 'precio_medio': float}}
             cantidad > 0 = largo, < 0 = corto (formato de schwab_client.obtener_posiciones)

    Retorna: {'adoptar': [(ticker, real)], 'soltar': [ticker],
              'ajustar': [(ticker, qty_interno_con_signo, qty_real_con_signo)]}
    """
    acciones = {"adoptar": [], "soltar": [], "ajustar": []}
    internos = {t: p for t, p in (interno or {}).items() if not p.get("reservado")}
    reales = real or {}

    for t in set(internos) | set(reales):
        i = internos.get(t)
        r = reales.get(t)
        if r and not i:
            acciones["adoptar"].append((t, r))
        elif i and not r:
            acciones["soltar"].append(t)
        elif i and r:
            dir_int = 1 if i.get("tipo") == "BUY" else -1
            qty_int = dir_int * int(i.get("cantidad", 0))
            if qty_int != int(r.get("cantidad", 0)):
                acciones["ajustar"].append((t, qty_int, int(r.get("cantidad", 0))))
    return acciones


# --- Auto-test rápido (python -m engine.reconciliacion) ---
if __name__ == "__main__":
    interno = {
        "NVDA": {"tipo": "BUY", "cantidad": 10},     # el bot cree que está largo
        "TSLA": {"tipo": "SELL", "cantidad": 5},     # el bot cree que está corto
        "AAPL": {"tipo": "BUY", "cantidad": 3},      # el humano lo cerró a mano
        "MSFT": {"reservado": True, "tipo": "BUY"},  # reserva en curso → ignorar
    }
    real = {
        "NVDA": {"cantidad": 10, "precio_medio": 120.0},   # coincide
        "TSLA": {"cantidad": -3, "precio_medio": 250.0},   # difiere cantidad (5→3)
        "META": {"cantidad": 4, "precio_medio": 500.0},    # abierta a mano
    }
    r = reconciliar(interno, real)
    assert r["adoptar"] == [("META", {"cantidad": 4, "precio_medio": 500.0})], r["adoptar"]
    assert r["soltar"] == ["AAPL"], r["soltar"]
    assert ("TSLA", -5, -3) in r["ajustar"], r["ajustar"]
    assert all(t != "NVDA" for t, *_ in r["ajustar"]), "NVDA coincide, no debe ajustarse"
    print("OK reconciliacion:", r)
