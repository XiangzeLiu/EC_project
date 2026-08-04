# Temporary Latency Diagnostics

This is temporary incident-investigation code. Remove all items listed below
after the Client-to-TS latency cause is confirmed:

- `Client/network/temp_latency_diagnostics.py`
- `Trader_Server/services/temp_latency_diagnostics.py`
- The temporary call sites in `Client/network/ts_websocket.py`,
  `Client/ui_qt/main_window.py`, and `Trader_Server/network/ws_server.py`.
- `Client/tools/collect_latency_diagnostics.ps1`
- `Client/tools/run_latency_diagnostics.bat`
- Client `%APPDATA%/SC Client/diagnostics/` output.
- TS `Trader_Server/data/logs/latency_diagnostics/` output.

The Client JSONL output intentionally omits the target address, token, account,
and broker data. The external route collector does contain network route data
and must be treated as administrator-only evidence.

Disable without code removal by setting:

```text
SC_CLIENT_TEMP_LATENCY_DIAGNOSTICS=0
TS_TEMP_LATENCY_DIAGNOSTICS=0
```
