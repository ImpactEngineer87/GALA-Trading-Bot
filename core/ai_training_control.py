"""Persisted operator switch for safe automatic SHADOW-only AI retraining.

This state is separate from config.yaml and all historical datasets. Merely
reading it never modifies anything. The operator can enable/disable after-
session retraining without stopping the trading engine or changing REAL modes.
Neither resolved hypothetical signals nor operator toggles bypass executed
close training gates or promote a model to PRODUCTION.
"""
from __future__ import annotations

from core.project_paths import resolve_runtime_path
from core.runtime_store import JsonState, EventHistory, utc_now


class AITrainingControl:
    def __init__(self, config=None):
        config = config or {}
        self.state = JsonState(resolve_runtime_path(
            config.get("state_path", "data/ai_training_control.json")
        ))
        self.history = EventHistory(resolve_runtime_path(
            config.get("audit_path", "data/ai_training_control_audit.jsonl")
        ))

    def policy(
        self, *, configured_enabled=False, configured_only_after_session=True,
        configured_auto_activate_production=False,
    ):
        """Read the effective policy; absence of an override honors old config."""
        saved = self.state.get("automatic_retraining")
        if saved is None:
            return {
                "enabled": bool(configured_enabled),
                "only_after_session": bool(configured_only_after_session),
                "auto_activate_production": bool(configured_auto_activate_production),
                "source": "CONFIG",
            }
        if not isinstance(saved, dict) or type(saved.get("enabled")) is not bool:
            raise ValueError("Invalid AI retraining control; automatic training blocked")
        return {
            "enabled": saved["enabled"],
            "only_after_session": True,
            "auto_activate_production": False,
            "source": "OPERATOR",
            "changed_at": str(saved.get("changed_at", "")),
            "actor": str(saved.get("actor", "")),
        }

    def set_enabled(self, enabled, actor):
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false")
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("Authenticated operator required")
        item = {
            "enabled": enabled,
            "only_after_session": True,
            "auto_activate_production": False,
            "changed_at": utc_now(),
            "actor": actor,
        }
        # Do not accept an unaudited policy change.
        self.history.append({
            "event": "AI_AUTO_SHADOW_RETRAINING_POLICY_CHANGED", **item,
        })
        self.state.put("automatic_retraining", item)
        return item
