"""Read operator-verified manual close evidence without changing trade labels."""
import json
import math
from pathlib import Path

from core.archived_accounting_resume import _trusted_approval_file


def load_resolutions(directory):
    path = Path(directory) / 'manual_accounting_resolutions.json'
    try:
        if not _trusted_approval_file(path):
            return {}
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema_version') != 1 or not isinstance(data.get('incidents'), dict):
            return {}
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def history_resolved(row, data):
    for incident in data.get('incidents', {}).values():
        if (incident.get('manual_fill_verified') is not True
                or incident.get('training_eligible') is not False
                or incident.get('performance_eligible') is not False):
            continue
        if any(row == original for original in incident.get('original_history_rows', [])):
            return True
    return False


def manual_risk_adjustment(directory, session_date):
    total = 0.0
    for incident in load_resolutions(directory).get('incidents', {}).values():
        if (incident.get('session_ny') != session_date
                or incident.get('manual_fill_verified') is not True
                or incident.get('training_eligible') is not False
                or incident.get('performance_eligible') is not False):
            continue
        value = incident.get('gross_pnl')
        if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value):
            total += value
    return total
