"""
Registration Client — 向 Server_manager 发起注册流程

完整实现 §4.6 节点注册与连接协议:
  阶段A: GET /ping          → 连通性测试
  阶段B: POST /register-request → 提交注册信息
  阶段C: GET /await-approval    → SSE 等待审核结果
  阶段D: 保存凭证              → config.json

支持重启恢复：读取 .register_state.json 继续 SSE 等待。
"""

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Callable

from ..config import (
    state, save_config, save_register_state,
    load_register_state, clear_register_state,
    DEFAULT_MANAGER_URL, DEFAULT_NODE_NAME, DEFAULT_REGION,
    DEFAULT_HOST, DEFAULT_CAPABILITIES, DEFAULT_CONTACT, DEFAULT_DESCRIPTION,
)

log = logging.getLogger("server_economic.registration")


def _get_url(path: str) -> str:
    """拼接管理端 URL"""
    return f"{state.manager_url.rstrip('/')}{path}"


# ── Step A: Ping ──────────────────────────────────────────────────────

def test_connection() -> tuple[bool, str]:
    """
    测试到 Server_manager 的连通性 (GET /ping)

    Returns:
        (成功与否, 消息)
    """
    url = _get_url("/ping")
    log.info(f"Testing connection: {url}")
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "pong":
                log.info(f"Connection OK: {data}")
                return True, "连接成功"
            return False, f"异常响应: {data}"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log.error(f"Ping HTTP error {e.code}: {body}")
        return False, f"HTTP {e.code}: {body[:80]}"
    except Exception as e:
        log.error(f"Ping failed: {e}")
        return False, str(e)


# ── Step B: Submit Registration ───────────────────────────────────────

def submit_registration(
    node_name: str | None = None,
    region: str | None = None,
    host: str | None = None,
    capabilities: list[str] | None = None,
    contact: str | None = None,
    description: str | None = None,
) -> dict | None:
    """
    提交注册请求到 Server_manager (POST /nodes/register-request)

    Args:
        节点信息字段，为 None 时使用默认值/配置值

    Returns:
        SM 返回的 JSON 字典（含 request_id），失败返回 None
    """
    payload = {
        "node_name": node_name or state.node_name or DEFAULT_NODE_NAME,
        "region": region or state.region or DEFAULT_REGION,
        "host": host or DEFAULT_HOST,
        "capabilities": capabilities or DEFAULT_CAPABILITIES,
        "contact": contact or DEFAULT_CONTACT,
        "description": description or DEFAULT_DESCRIPTION,
    }

    # 更新运行时状态
    state.node_name = payload["node_name"]
    state.region = payload["region"]
    state.status = "registering"

    url = _get_url("/nodes/register-request")
    json_payload = json.dumps(payload).encode("utf-8")

    log.info(f"Submitting registration to {url}")
    log.info(f"Node info: name={payload['node_name']}, region={payload['region']}")

    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=json_payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))

            if result.get("ok") and result.get("request_id"):
                request_id = result["request_id"]
                expire_at = result.get("expire_at", "")

                # 保存临时状态，支持重启恢复
                save_register_state(
                    request_id=request_id,
                    manager_url=state.manager_url,
                    node_name=payload["node_name"],
                    expire_at=expire_at,
                )

                log.info(f"Registration submitted: {request_id}, expires at {expire_at}")
                return result
            else:
                log.error(f"Registration rejected: {result}")
                return None

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log.error(f"Registration HTTP error {e.code}: {body}")
        return None
    except Exception as e:
        log.error(f"Registration failed: {e}")
        return None


# ── Step C: SSE Wait for Approval ─────────────────────────────────────

def await_approval(
    request_id: str | None = None,
    timeout: int = 3600,
    on_status_update: Callable[[str], None] | None = None,
    shutdown_check: Callable[[], bool] | None = None,
) -> dict | None:
    """
    通过 SSE 等待管理员审核结果 (GET /nodes/await-approval)

    Args:
        request_id: 请求 ID，为 None 时尝试从本地状态文件读取
        timeout: 最大等待时间（秒）
        on_status_update: 状态更新回调（如 UI 显示等待时间）
        shutdown_check: 关闭检查回调

    Returns:
        审核结果字典 {approved, server_id, token, ...}，
        超时/断开返回 None
    """
    if not request_id:
        reg_state = load_register_state()
        if not reg_state:
            log.error("No request_id available and no saved state found")
            return None
        request_id = reg_state["request_id"]

    import urllib.parse
    params = urllib.parse.urlencode({"request_id": request_id})
    url = _get_url(f"/nodes/await-approval?{params}")

    log.info(f"Opening SSE connection: {url} (timeout={timeout}s)")

    req = urllib.request.Request(url)
    start_time = time.time()
    buffer = ""

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            while True:
                # 检查关闭信号
                if shutdown_check and shutdown_check():
                    log.info("Shutdown requested during SSE wait")
                    return None

                chunk = resp.read(1).decode("utf-8", errors="replace")
                if not chunk:
                    break
                buffer += chunk

                # SSE 协议：双换行分隔事件
                while "\n\n" in buffer:
                    event_block, buffer = buffer.split("\n\n", 1)
                    event_data = ""
                    for line in event_block.split("\n"):
                        line = line.rstrip("\r")
                        if line.startswith("data:"):
                            event_data = line[len("data:"):].strip()

                    if event_data:
                        result = json.loads(event_data)
                        approved = result.get("approved", False)

                        if approved:
                            log.info(f"★ APPROVED! server_id={result.get('server_id')}")
                            # 保存凭证
                            save_config({
                                "server_id": result.get("server_id"),
                                "token": result.get("token"),
                                "manager_url": state.manager_url,
                                "node_name": state.node_name,
                                "region": state.region,
                            })
                            # 清理临时状态
                            clear_register_state()

                            # 更新运行时状态
                            state.server_id = result.get("server_id", "")
                            state.token = result.get("token", "")
                            state.status = "approved"

                        else:
                            reason = result.get("reason", "")
                            log.warning(f"REJECTED: {reason}")
                            clear_register_state()
                            state.status = "rejected"

                        return result

                # SSE 心跳注释行 — 报告等待进度
                if ": heartbeat" in buffer:
                    elapsed = int(time.time() - start_time)
                    log.debug(f"SSE waiting... {elapsed}s elapsed")
                    if on_status_update:
                        on_status_update(f"已等待 {elapsed}s")
                    buffer = ""

    except KeyboardInterrupt:
        log.info("SSE wait interrupted by user")
        return None
    except Exception as e:
        log.error(f"SSE connection error: {e}")
        return None


# ── 完整注册流程（编排）──────────────────────────────────────────────

def run_full_registration(
    manager_url: str | None = None,
    **reg_kwargs,
) -> bool:
    """
    执行完整的注册流程：ping → register → SSE wait → save credentials

    Args:
        manager_url: SM 地址
        **reg_kwargs: 传递给 submit_registration 的参数

    Returns:
        是否注册成功
    """
    if manager_url:
        state.manager_url = manager_url

    # Step A: 测试连通性
    ok, msg = test_connection()
    if not ok:
        log.error(f"Cannot reach Server_manager at {state.manager_url}: {msg}")
        state.status = "error"
        return False

    # Step B: 提交注册
    reg_result = submit_registration(**reg_kwargs)
    if not reg_result:
        log.error("Registration submission failed")
        state.status = "error"
        return False

    request_id = reg_result["request_id"]

    # Step C: SSE 等待审核
    approval_result = await_approval(request_id=request_id)
    if not approval_result or not approval_result.get("approved"):
        log.error("Registration not approved")
        state.status = "rejected" if approval_result else "error"
        return False

    log.info("=" * 50)
    log.info("REGISTRATION COMPLETE!")
    log.info(f"  server_id : {state.server_id}")
    log.info(f"  token     : {state.token[:20]}...")
    log.info(f"  manager   : {state.manager_url}")
    log.info("=" * 50)
    state.status = "approved"
    return True


# ── 启动时自动恢复检查 ─────────────────────────────────────────────────

def check_and_restore_session() -> bool:
    """
    检查是否有已保存的注册凭证或未完成的注册状态：
    1. 有有效 config.json → 直接加载凭证，跳过注册
    2. 有 .register_state.json 且未过期 → 尝试恢复 SSE 等待
    3. 都没有 → 返回 False，需要全新注册

    Returns:
        是否已拥有可用凭证
    """
    from ..config import load_config, is_registered

    # Case 1: 已有有效凭证
    cfg = load_config()
    if is_registered():
        state.server_id = cfg["server_id"]
        state.token = cfg["token"]
        state.manager_url = cfg.get("manager_url", DEFAULT_MANAGER_URL)
        state.node_name = cfg.get("node_name", DEFAULT_NODE_NAME)
        state.region = cfg.get("region", DEFAULT_REGION)
        state.status = "approved"
        log.info(f"Restored session: server_id={state.server_id}")
        return True

    # Case 2: 有未完成的注册 → 需要调用 await_approval 恢复
    reg_state = load_register_state()
    if reg_state:
        state.manager_url = reg_state.get("manager_url", DEFAULT_MANAGER_URL)
        state.node_name = reg_state.get("node_name", DEFAULT_NODE_NAME)
        state.status = "registering"
        log.info(f"Found pending registration: {reg_state['request_id']}, "
                 f"will need to resume SSE wait")
        return False  # 调用方需要决定是否恢复

    # Case 3: 全新节点
    log.info("No existing session found, full registration required")
    state.status = "uninitialized"
    return False
