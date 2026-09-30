"""
=============================================================
PAQUETE: agents
PROYECTO: GALA6 Chocoletra Bot
DESCRIPCIÓN:
    Sistema dinámico de 3 Agentes de IA basado en Smart Money
    Concepts (SMC). Cruza el historial multi-temporal de cada
    acción con las alertas matemáticas de core/strategy.py.

    Flujo de decisión y confluencia:
      Agente 1 (Macro/Liquidez)  → Dirección del Día
      Agente 2 (Micro/FVG)       → Propuesta de operación
      Agente 3 (Auditor/Riesgo)  → Aprobación o veto final

    El Orquestador coordina el flujo y devuelve una DecisionFinal.
    El Entrenador aplica aprendizaje continuo sobre los resultados
    reales de cada decisión (meta-modelo compartido entre acciones).
=============================================================
"""

from agents.contracts import (
    Direccion,
    Veredicto,
    DatosMultiTemporal,
    NivelLiquidez,
    FairValueGap,
    DireccionDia,
    PropuestaOperacion,
    VeredictoAuditor,
    DecisionFinal,
)

__all__ = [
    "Direccion",
    "Veredicto",
    "DatosMultiTemporal",
    "NivelLiquidez",
    "FairValueGap",
    "DireccionDia",
    "PropuestaOperacion",
    "VeredictoAuditor",
    "DecisionFinal",
]
