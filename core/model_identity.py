"""Model identity and operator authorization pinned to exact artifact bytes."""
import hashlib
import math
from pathlib import Path
from core.runtime_store import JsonState, EventHistory, utc_now


def model_snapshot(meta, path='', loaded=False, tier='PRODUCTION'):
    version = meta.get('model_version') or meta.get('evaluation_id')
    if path and Path(path).is_file():
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        version = digest  # changing artifact bytes always invalidates authorization
    return {'model_version': version, 'model_tier': tier,
            'training_date': meta.get('trained_utc') or meta.get('entrenado_utc') or meta.get('fecha'),
            'validation_date': meta.get('evaluation_utc'),
            'balanced_accuracy': meta.get('balanced_accuracy'),
            'threshold': meta.get('decision_threshold'),
            'production_status': ('MANUAL_OVERRIDE' if meta.get('manual_override') else 'APPROVED') if loaded and tier == 'PRODUCTION' else ('SHADOW' if loaded else 'UNAVAILABLE'),
            'feature_version': meta.get('feature_version'), 'loaded': loaded}


class ManualRealAuthorization:
    def __init__(self, config):
        self.enabled = bool(config.get('enabled', False))
        self.state = JsonState(config.get('state_path', 'data/real_authorization.json'))
        self.history = EventHistory(config.get('audit_path', 'data/real_authorization_audit.jsonl'))

    def authorize(self, ticker, version, reason, actor, bypass_probability=False):
        if not self.enabled or not version or not str(reason).strip() or not str(actor).strip():
            raise ValueError('Enabled authorization, loaded model, operator, and reason are required')
        row = {'ticker': ticker, 'model_version': version, 'reason': reason,
               'actor': actor, 'bypass_probability': bool(bypass_probability), 'authorized_utc': utc_now()}
        self.history.append({'event': 'AUTHORIZE_REAL', **row})
        self.state.put(ticker, row)
        return row

    def revoke(self, ticker, actor):
        self.history.append({'event': 'REVOKE_REAL', 'ticker': ticker, 'actor': actor})
        self.state.put(ticker, None)

    def active(self, ticker, version):
        if not self.enabled or not version:
            return None
        row = self.state.get(ticker)
        return row if row and row.get('model_version') == version else None

    def approve_below_threshold(self, ticker, version, probability, threshold):
        row = self.active(ticker, version)
        if not row or not row.get('bypass_probability'):
            return None
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            return None
        # Audit must succeed before authorization affects the decision.
        self.history.append({'event': 'PROBABILITY_OVERRIDE_APPLIED', **row,
                             'probability': probability, 'threshold': threshold})
        return row
