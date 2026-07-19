"""
API Client — 与 Trader_Server 后端通信的 HTTP 客户端
封装对 /api/* 端点的所有调用
"""

import json
import urllib.request
import urllib.error

from Trader_Server.config import DEFAULT_WS_PORT


class TSApiClient:
    """Trader_Server Desktop GUI 专用 API 客户端"""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_WS_PORT):
        self.base_url = f"http://{host}:{port}"

    # ── 基础请求 ──────────────────────────────────────────────

    def _request(self, method: str, path: str, body=None, timeout: int = 15) -> dict | None:
        url = self.base_url + path
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode())
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}", "error_type": "HTTP_ERROR"}
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            msg = str(reason)
            if isinstance(reason, ConnectionRefusedError) or "WinError 10061" in msg:
                return {
                    "ok": False,
                    "error": f"本地 TS 服务不可达: {self.base_url}（请确认子节点服务已启动）",
                    "error_type": "TS_LOCAL_UNREACHABLE",
                    "raw_error": str(e),
                }
            return {"ok": False, "error": str(e), "error_type": "URL_ERROR"}
        except Exception as e:
            return {"ok": False, "error": str(e), "error_type": "UNKNOWN_ERROR"}

    def get(self, path: str, timeout: int = 15) -> dict | None:
        return self._request("GET", path, timeout=timeout)

    def post(self, path: str, body: dict, timeout: int = 15) -> dict | None:
        return self._request("POST", path, body, timeout=timeout)


    # ── 业务 API ─────────────────────────────────────────────

    def get_status(self, timeout: int = 5) -> dict | None:
        return self.get("/api/status", timeout=timeout)


    def get_economic_data(self) -> dict | None:
        return self.get("/api/economic-data")

    def get_logs(self, limit: int = 100) -> dict | None:
        return self.get(f"/api/logs?limit={limit}")

    def ping_local(self) -> dict | None:
        # 本地健康检查用 /health（返回 {"status":"ok"}）
        r = self.get("/health", timeout=3)
        if r and isinstance(r, dict) and r.get("status") == "ok":
            return {"ok": True}
        if r and isinstance(r, dict) and r.get("error"):
            return r
        return {"ok": False, "error": "本地服务响应异常", "error_type": "TS_LOCAL_BAD_RESPONSE"}


    def ping_sm(self, manager_url: str) -> dict | None:
        return self.post("/api/register/ping", {"manager_url": manager_url})


    def submit_registration(self, payload: dict) -> dict | None:
        return self.post("/api/register/submit", payload)

    def cancel_registration(self, request_id: str, manager_url: str = "") -> dict | None:
        return self.post("/api/register/cancel", {
            "request_id": request_id,
            "manager_url": manager_url,
            "reason": "node_cancelled_by_user",
            "force_discard_approved": True,
        }, timeout=8)


    def clear_credentials(self) -> dict | None:
        return self.post("/api/register/clear", {})


    # ── SSE 流式读取（用于等待审批）──────────────────────────




    def sse_await_approval(self, request_id: str):
        """
        ??????????? yield ?? SSE ???
        ????????? resp.read(4096) ?????????????
        ?????????????????????
        """
        import urllib.parse

        def _parse_event(lines: list[str]) -> dict | None:
            if not lines:
                return None
            data_lines: list[str] = []
            for raw_line in lines:
                if raw_line.startswith("data:"):
                    data_lines.append(raw_line[5:].strip())
            if not data_lines:
                return None
            data_str = "\\n".join(data_lines).strip()
            if not data_str:
                return None
            try:
                return json.loads(data_str)
            except ValueError:
                return {"raw": data_str}

        params = urllib.parse.urlencode({"request_id": request_id})
        url = f"{self.base_url}/api/register/await-approval?{params}"
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        event_lines: list[str] = []
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
                while True:
                    raw_line = resp.readline()
                    if not raw_line:
                        parsed = _parse_event(event_lines)
                        if parsed is not None:
                            yield parsed
                        break

                    text_line = raw_line.decode("utf-8", errors="replace").rstrip("\\r\\n")
                    if text_line == "":
                        parsed = _parse_event(event_lines)
                        if parsed is not None:
                            yield parsed
                        event_lines = []
                    else:
                        event_lines.append(text_line)
        except Exception as e:
            yield {"approved": False, "reason": f"SSE error: {e}"}
