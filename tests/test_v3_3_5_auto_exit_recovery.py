from types import SimpleNamespace

from api.hybrid_order_executor import EjecutorHibridoDeOrdenes


def test_working_snapshot_prefers_shared_schwab_provider_and_accepts_empty_list():
    ex = EjecutorHibridoDeOrdenes(False, config={})
    calls = []
    ex.set_working_orders_provider(lambda account: calls.append(account) or [])

    class ForbiddenSession:
        def get(self, *args, **kwargs):
            raise AssertionError("raw working-order HTTP path must not be used")

    ex._session = ForbiddenSession()
    assert ex._working_orders_snapshot("acct-hash") == []
    assert calls == ["acct-hash"]


def test_working_snapshot_provider_none_is_unknown_fail_closed():
    ex = EjecutorHibridoDeOrdenes(False, config={})
    ex.set_working_orders_provider(lambda account: None)
    assert ex._working_orders_snapshot("acct-hash") is None


def test_working_snapshot_provider_returns_current_orders():
    ex = EjecutorHibridoDeOrdenes(False, config={})
    rows = [{"orderId": 123, "status": "WORKING"}]
    ex.set_working_orders_provider(lambda account: rows)
    assert ex._working_orders_snapshot("acct-hash") is rows
