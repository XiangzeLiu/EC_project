"""
HTTP Client
封装 urllib.request，提供同步HTTP通信能力
用于认证、订单操作、数据查询等REST API调用
"""

import json
import urllib.request
import urllib.error

from ..constants import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT


class HttpClient:
    """HTTP REST API 客户端"""

    def __init__(self, host: str = DEFAULT_SERVER_HOST, port: int = DEFAULT_SERVER_PORT):
        self.base_url = f"http://{host}:{port}"
        self._token: str = ""

    @property
    def token(self) -> str:
        return self._token

    @token.setter
    def token(self, value: str):
        self._token = value

    @property
    def is_connected(self) -> bool:
        return bool(self._token)

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        """
        发送HTTP请求

        Args:
            method: HTTP方法 (GET/POST/DELETE)
            path: API路径 (如 /auth/login)
            body: 请求体(POST时使用)

        Returns:
            (status_code, response_dict) 元组
        """
        url = self.base_url + path
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            try:
                resp_body = json.loads(e.read().decode())
            except Exception:
                resp_body = {}
            return e.code, resp_body
        except Exception as e:
            return 0, {"detail": str(e)}

    def get(self, path: str) -> tuple[int, dict]:
        return self.request("GET", path)

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        return self.request("POST", path, body)

    def delete(self, path: str) -> tuple[int, dict]:
        return self.request("DELETE", path)

    def health_check(self) -> bool:
        """检查服务器是否可达"""
        status, _ = self.get("/health")
        return status == 200
