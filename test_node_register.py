#!/usr/bin/env python3
"""
Server_economic 节点模拟器
模拟子服务端向 Server_manager 发起注册 → 审核 → 持续心跳保活的完整生命周期

使用方式:
    python test_node_register.py [--url http://127.0.0.1:8800] [--interval 30]

流程：
    Step 1: GET  /ping                  → 测试连通性
    Step 2: POST /nodes/register-request  → 提交注册信息
    Step 3: GET  /nodes/await-approval    → SSE 等待审核（阻塞）
            （此时需管理员通过 Web/API 审批）
    Step 4: 收到审核结果 → 显示 server_id + token
    Step 5: 循环 POST /nodes/heartbeat   → 持续心跳保活（按 interval 间隔）
            Ctrl+C 可优雅退出
"""

import argparse
import json
import signal
import sys
import time
import urllib.request
import urllib.error
import urllib.parse


# ── 配置 ──────────────────────────────────────────────────────────────────

DEFAULT_URL = "http://127.0.0.1:8800"
DEFAULT_HEARTBEAT_INTERVAL = 30  # 秒，与服务端 next_interval 保持一致

TEST_NODE_INFO = {
    "node_name": "economic-us",
    "region": "US",
    "host": "10.0.1.100",
    "capabilities": ["cpi", "gdp", "interest_rate", "employment"],
    "contact": "admin@example.com",
    "description": "美国经济数据采集节点（测试）",
}

# 全局退出标志
_shutdown = False


def _sigint_handler(signum, frame):
    global _shutdown
    if _shutdown:
        print("\n强制退出!")
        sys.exit(1)
    _shutdown = True
    print("\n\n收到退出信号，将在下次心跳后停止 (再按一次 Ctrl+C 强制退出)...")


signal.signal(signal.SIGINT, _sigint_handler)
# Windows 兼容
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _sigint_handler)


# ── HTTP 工具函数 ────────────────────────────────────────────────────────

def http_get(base_url: str, path: str) -> dict:
    """发送 GET 请求，返回解析后的 JSON"""
    url = f"{base_url}{path}"
    print(f"  GET  {url}")
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"  ← {json.dumps(data, ensure_ascii=False, indent=2)}")
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  ✗ HTTP {e.code}: {body}")
        return {"error": f"HTTP {e.code}", "detail": body}
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return {"error": str(e)}


def http_post_json(base_url: str, path: str, body: dict,
                   headers: dict | None = None, silent: bool = False) -> dict:
    """发送 POST JSON 请求"""
    url = f"{base_url}{path}"
    payload = json.dumps(body).encode("utf-8")
    hds = {"Content-Type": "application/json"}
    if headers:
        hds.update(headers)

    if not silent:
        print(f"  POST {url}")
        print(f"  → Body: {json.dumps(body, ensure_ascii=False)}")

    try:
        req = urllib.request.Request(url, data=payload, headers=hds, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not silent:
                print(f"  ← {json.dumps(data, ensure_ascii=False, indent=2)}")
            return data
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"  ✗ HTTP {e.code}: {body_text}" if not silent else f"  ✗ HTTP {e.code}: {body_text[:80]}")
        return {"error": f"HTTP {e.code}", "detail": body_text}
    except Exception as e:
        print(f"  ✗ Error: {e}" if not silent else f"  ✗ {e}")
        return {"error": str(e)}


# ── SSE 连接 ───────────────────────────────────────────────────────────

def sse_connect(base_url: str, request_id: str, timeout: int = 300):
    """
    连接 SSE 端点等待审核结果
    返回解析后的 JSON dict 或超时返回 None
    """
    url = f"{base_url}/nodes/await-approval?{urllib.parse.urlencode({'request_id': request_id})}"
    print(f"\n  SSE  {url}  (等待审核结果...)")
    print(f"  ════════════════════════════════════════════════════")
    print(f"  ⏳ 等待管理员操作...")
    print(f"     请在 Web 管理 UI 中操作审批")
    print(f"  ════════════════════════════════════════════════════\n")

    req = urllib.request.Request(url)
    start_time = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            buffer = ""
            while True:
                chunk = resp.read(1).decode("utf-8", errors="replace")
                if not chunk:
                    break
                buffer += chunk

                # 按 SSE 协议解析：双换行分隔事件
                while "\n\n" in buffer:
                    event_block, buffer = buffer.split("\n\n", 1)
                    event_data = ""
                    for line in event_block.split("\n"):
                        line = line.rstrip("\r")
                        if line.startswith("data:"):
                            event_data = line[len("data:"):].strip()
                    if event_data:
                        return json.loads(event_data)

                # 检测心跳注释行并打印等待时长
                if ": heartbeat" in buffer:
                    elapsed = int(time.time() - start_time)
                    sys.stdout.write(f"\r  ♥ 已等待 {elapsed}s ...         ")
                    sys.stdout.flush()
                    buffer = ""
    except KeyboardInterrupt:
        print("\n  ✗ 用户取消等待")
        return None
    except Exception as e:
        print(f"\n  ✗ SSE 连接断开/超时: {e}")
        return None


# ── 流程步骤 ────────────────────────────────────────────────────────────

def step1_ping(base_url):
    """Step 1: 测试连通性"""
    print("=" * 60)
    print("Step 1: 测试连通性 (GET /ping)")
    print("=" * 60)
    result = http_get(base_url, "/ping")
    if result.get("status") == "pong":
        print("  ✓ 连通性测试通过\n")
        return True
    else:
        print("  ✗ 连通性测试失败\n")
        return False


def step2_register(base_url):
    """Step 2: 提交注册请求"""
    print("=" * 60)
    print("Step 2: 提交注册请求 (POST /nodes/register-request)")
    print("=" * 60)
    result = http_post_json(base_url, "/nodes/register-request", TEST_NODE_INFO)
    if result.get("ok") and result.get("request_id"):
        print(f"  ✓ 注册请求已提交: {result['request_id']}\n")
        return result["request_id"]
    else:
        print(f"  ✗ 提交失败: {result}\n")
        return None


def step3_await_approval(base_url, request_id):
    """Step 3: SSE 等待审核"""
    print("=" * 60)
    print(f"Step 3: SSE 等待审核结果 (GET /nodes/await-approval)")
    print("=" * 60)

    result = sse_connect(base_url, request_id)

    if result is None:
        print("  ✗ 未收到审核结果（连接中断或超时）\n")
        return None

    approved = result.get("approved", False)
    if approved:
        print(f"\n  ★★★ 审核通过！ ★★★")
        print(f"     server_id : {result.get('server_id')}")
        print(f"     token     : {result.get('token', '')[:20]}...")
        print()
    else:
        print(f"\n  ✗ 审核被拒绝: {result.get('reason', '未知原因')}\n")

    return result


# ── 持续心跳循环 ───────────────────────────────────────────────────────

def heartbeat_loop(base_url: str, server_id: str, token: str,
                   interval: int = DEFAULT_HEARTBEAT_INTERVAL):
    """
    持续发送心跳保活
    - 每 interval 秒发送一次 POST /nodes/heartbeat
    - 失败时指数退避重试（最大 60s）
    - 支持 Ctrl+C 优雅退出
    - 打印心跳统计信息
    """
    global _shutdown

    print("=" * 60)
    print("Step 4: 进入心跳保活循环")
    print("=" * 60)
    print(f"  server_id  : {server_id}")
    print(f"  token      : {token[:16]}...")
    print(f"  间隔       : {interval}s")
    print(f"  退出方式   : Ctrl+C")
    print(f"  ──────────────────────────────────────────────\n")

    seq = 0          # 心跳序号
    ok_count = 0     # 成功次数
    fail_count = 0   # 失败次数
    backoff = 1      # 当前退避秒数

    while not _shutdown:
        seq += 1
        now_str = time.strftime("%H:%M:%S")

        result = http_post_json(
            base_url,
            "/nodes/heartbeat",
            {"ts": int(time.time()), "ip": TEST_NODE_INFO.get("host", "")},
            headers={"Authorization": f"Bearer {token}"},
            silent=True,
        )

        status = result.get("status", "")
        if status == "ok":
            ok_count += 1
            backoff = 1  # 成功后重置退避
            next_interval = result.get("next_interval", interval)
            sys.stdout.write(
                f"  [{now_str}] ♥ #{seq:04d} 心跳成功  "
                f"(累计 OK={ok_count}  FAIL={fail_count})  "
                f"next_interval={next_interval}s    \r"
            )
            sys.stdout.flush()

            # 使用服务器返回的 interval 或默认值
            wait = min(next_interval or interval, 120)
        else:
            fail_count += 1
            err_msg = result.get("message") or result.get("detail") or str(result.get("error", ""))
            print(
                f"\n  [{now_str}] ✗ #{seq:04d} 心跳失败  "
                f"(累计 OK={ok_count}  FAIL={fail_count})  "
                f"原因: {err_msg[:60]}"
            )
            wait = backoff
            backoff = min(backoff * 2, 60)  # 指数退避，最大 60s

        # 分段 sleep 以便响应 Ctrl+C
        deadline = time.time() + wait
        while time.time() < deadline and not _shutdown:
            time.sleep(min(1.0, deadline - time.time()))

    # ── 退出汇总 ──
    print(f"\n\n{'=' * 60}")
    print("心跳循环已停止")
    print(f"{'=' * 60}")
    print(f"  总发送次数 : {seq}")
    print(f"  成功次数   : {ok_count}")
    print(f"  失败次数   : {fail_count}")
    print(f"  server_id  : {server_id}")
    print()


# ── 入口 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Server_economic 节点模拟器 — 注册 + 持续心跳",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python test_node_register.py                          # 默认连接 localhost:8800\n"
            "  python test_node_register.py --url http://192.168.1.100:8800\n"
            "  python test_node_register.py --interval 10             # 每 10s 发一次心跳\n"
            "  python test_node_register.py --skip-ping               # 跳过连通性检测\n"
        ),
    )
    parser.add_argument("--url", default=DEFAULT_URL,
                        help=f"Server_manager 地址 (默认: {DEFAULT_URL})")
    parser.add_argument("--skip-ping", action="store_true",
                        help="跳过 ping 连通性测试")
    parser.add_argument("--interval", type=int, default=DEFAULT_HEARTBEAT_INTERVAL,
                        help=f"心跳间隔秒数 (默认: {DEFAULT_HEARTBEAT_INTERVAL})")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")

    print()
    print("╔═════════════════════════════════════════════════════════╗")
    print("║     Server_economic 节点模拟器 (可持续心跳版)           ║")
    print("║     目标 Server_manager: {:<33}║".format(base_url))
    print("╚═════════════════════════════════════════════════════════╝")
    print()

    # Step 1: Ping
    if not args.skip_ping:
        if not step1_ping(base_url):
            print("无法连接到 Server_manager，请确认服务已启动。")
            sys.exit(1)

    # Step 2: Register
    request_id = step2_register(base_url)
    if not request_id:
        sys.exit(1)

    # Step 3: Await Approval (SSE 阻塞)
    approval_result = step3_await_approval(base_url, request_id)

    if not approval_result or not approval_result.get("approved"):
        print("\n流程终止：注册未获批准。")
        sys.exit(0)

    # Step 4+: 持续心跳循环
    server_id = approval_result.get("server_id", "")
    token = approval_result.get("token", "")

    if token:
        heartbeat_loop(base_url, server_id, token, interval=args.interval)
    else:
        print("错误：未获取到 token，无法进行心跳。")


if __name__ == "__main__":
    main()
