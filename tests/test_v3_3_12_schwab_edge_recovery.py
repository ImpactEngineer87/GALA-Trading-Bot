from pathlib import Path

from api.schwab_oauth import build_safe_diagnostic
import requests


def _response(status, text):
    r = requests.Response()
    r.status_code = status
    r.encoding = 'utf-8'
    r._content = text.encode('utf-8')
    return r


def test_edge_reference_is_extracted_and_action_requires_network_change():
    r = _response(403, '<HTML><TITLE>Access Denied</TITLE>Reference #18.abc123 https://errors.edgesuite.net/18.abc123</HTML>')
    d = build_safe_diagnostic(r, phase='authorization_code_exchange', client_id='client-ABCD', redirect_uri='https://127.0.0.1')
    assert d['edge_access_denied'] is True
    assert d['edge_reference'] == '18.abc123'
    assert d['recommended_action'] == 'CHANGE_NETWORK_THEN_NEW_CODE'
    assert d['network_recovery_required'] is True


def test_v3312_network_diagnostic_and_circuit_breaker_are_present():
    server = Path('server.py').read_text(encoding='utf-8')
    client = Path('api/schwab_client.py').read_text(encoding='utf-8')
    html = Path('dashboard/index.html').read_text(encoding='utf-8')
    assert "@app.route('/api/schwab/network-diagnostics', methods=['POST'])" in server
    assert 'schwab_edge_denial_count' in server
    assert 'schwab_edge_circuit_until_monotonic' in server
    assert '_oauth_edge_circuit_until' in client
    assert 'timedelta(minutes=5)' in client
    assert 'Diagnosticar red' in html
    assert 'Trading Algorítmico v3.3.12' in html
