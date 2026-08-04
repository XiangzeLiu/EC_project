from types import SimpleNamespace

from Client.network.ts_websocket import TSWebSocketClient
from Client.services.trading_session import TradingSession, sanitize
from Trader_Server.services.client_security import safe_order_record
from Trader_Server.services.config_sync import _client_status


def test_client_message_sanitizer_removes_direct_resource_information():
    message = (
        "Interactive Brokers interactive_brokers IB IBKR TT Gateway wss://www.ts01.scjrdomain.com/ws "
        "127.0.0.1:4001 [::1]:4002 account U1234567 client_id=7 session=sess_abc trace=trc_def"
    )
    sanitized = sanitize(message)
    lowered = sanitized.lower()
    for forbidden in ("interactive brokers", "interactive_brokers", " ib ", "ibkr", " tt ", "gateway", "scjrdomain.com", "127.0.0.1", "::1", "4001", "4002", "u1234567", "client_id", "sess_abc", "trc_def"):
        assert forbidden not in lowered


def test_client_status_normalization_discards_provider_and_account_identity():
    session = TradingSession(SimpleNamespace())
    detail = session.set_broker_detail({
        "broker_type": "interactive_brokers",
        "connected": True,
        "capabilities": {"quotes": True, "orders": False},
        "gateway": {"host": "127.0.0.1", "port": 4001, "client_id": 1},
        "account": {"account_id": "U1234567", "nickname": "private", "authority_level": "read-only"},
        "order_options": {"default_route": "SMART", "routes": ["SMART"], "route_editable": True, "hidden_order": True},
    })
    assert "broker_type" not in detail
    assert "gateway" not in detail
    assert detail["account"] == {"authority_level": "read-only"}
    assert detail["read_only"] is True
    assert detail["order_options"]["hidden_order"] is True


def test_ts_client_status_is_a_minimum_safe_projection():
    public = _client_status({
        "broker_type": "interactive_brokers",
        "connected": True,
        "config_version": 8,
        "capabilities": {"quotes": True, "orders": True},
        "gateway": {"host": "127.0.0.1", "port": 4001, "client_id": 1},
        "account": {"account_id": "U1234567", "authority_level": "full"},
        "order_options": {"default_route": "SMART", "routes": ["SMART", "ARCA"], "route_editable": True, "hidden_order": True},
        "error": {},
    })
    assert set(public) == {"connected", "capabilities", "read_only", "account", "order_options", "error"}
    assert public["account"] == {"authority_level": "full"}
    assert public["order_options"]["routes"] == ["SMART", "ARCA"]


def test_order_record_keeps_business_fields_and_removes_resource_fields():
    safe = safe_order_record({
        "id": "701",
        "symbol": "AAPL",
        "status": "Rejected",
        "status_message": "IB Gateway price too far from NBBO",
        "account_id": "U1234567",
        "broker_type": "interactive_brokers",
    })
    assert safe["id"] == "701"
    assert safe["symbol"] == "AAPL"
    assert safe["status_message"] == "订单价格超出允许范围"
    assert "account_id" not in safe
    assert "broker_type" not in safe


def test_production_endpoint_policy_accepts_pool_and_rejects_external_targets():
    TSWebSocketClient.validate_production_endpoint("wss://www.ts01.scjrdomain.com/ws")
    for endpoint in ("ws://www.ts01.scjrdomain.com/ws", "wss://127.0.0.1:4001/ws", "wss://example.com/ws"):
        try:
            TSWebSocketClient.validate_production_endpoint(endpoint)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe endpoint accepted: {endpoint}")
