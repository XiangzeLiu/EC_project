"""
Tastytrade SDK Service
封装 Tastytrade SDK 操作：Session管理、下单、撤单、查询持仓/订单
"""

import logging

from config import session_store, log

# SDK 导入标记
SDK_OK = False
try:
    from tastytrade import Session
    from tastytrade.account import Account
    SDK_OK = True
except ImportError as e:
    log.warning(f"Tastytrade SDK not available: {e}")


async def _create_session_account() -> tuple:
    """
    使用环境变量中的 secret+token 创建 Session 和 Account

    Returns:
        (Session, Account) 或抛出异常
    """
    if not SDK_OK:
        raise RuntimeError("Tastytrade SDK is not installed. Run: pip install tastytrade")

    if not session_store.get("secret") or not session_store.get("token"):
        raise RuntimeError(
            "TASTY_SECRET and TASTY_TOKEN environment variables are required"
        )

    s = Session(session_store["secret"], session_store["token"])
    accts = await Account.get(s)

    # 优先使用配置中指定的账户号，否则取第一个
    target_acct = session_store.get("acct_num", "")
    if target_acct:
        a = next(
            (x for x in accts if str(x.account_number) == target_acct),
            accts[0] if accts else None,
        )
    else:
        a = accts[0] if accts else None

    if not a:
        raise RuntimeError("No accounts found in Tastytrade session")

    return s, a


async def get_session_account() -> tuple:
    """获取缓存的或新建的 Session/Account（供路由层调用）"""
    from session_cache import get_fresh
    return await get_fresh()


def serialize_order(o) -> dict:
    """
    将 Tastytrade Order 对象序列化为字典
    用于 API 响应
    """
    try:
        leg = o.legs[0] if o.legs else None

        legs_data = []
        for l in (o.legs or []):
            fills_data = []
            for f in (getattr(l, "fills", []) or []):
                fills_data.append({
                    "fill_price": str(getattr(f, "fill_price", 0) or 0),
                    "quantity":   str(getattr(f, "quantity", 0) or 0),
                    "filled_at":  str(getattr(f, "filled_at", "") or ""),
                })
            legs_data.append({
                "symbol":   str(l.symbol),
                "action":   str(l.action),
                "quantity": str(l.quantity),
                "fills":    fills_data,
            })

        return {
            "id":         str(o.id),
            "symbol":     leg.symbol if leg else "\u2014",
            "action":     str(leg.action) if leg else "\u2014",
            "qty":        str(leg.quantity) if leg else "\u2014",
            "price":      f"{abs(float(o.price)):.2f}" if o.price else "MKT",
            "type":       str(o.order_type).split(".")[-1] if o.order_type else "\u2014",
            "tif":        str(o.time_in_force).split(".")[-1]
                          if hasattr(o, "time_in_force") else "\u2014",
            "status":     str(o.status).split(".")[-1] if o.status else "\u2014",
            "updated_at": str(getattr(o, "updated_at", "") or ""),
            "legs":       legs_data,
        }
    except Exception as e:
        log.warning(f"Serialize order failed: {e}")
        return {}
