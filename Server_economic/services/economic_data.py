"""
Economic Data Service — 经济数据采集业务逻辑

提供模拟经济指标数据的采集、缓存和查询能力。
生产环境中可替换为真实数据源接入（API爬取、数据库查询等）。

当前支持的指标（capabilities）:
  - cpi             : 居民消费价格指数
  - gdp             : 国内生产总值
  - interest_rate   : 利率基准
  - employment      : 就业数据
  - trade_balance   : 贸易差额
"""

import logging
import random
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from ..config import state

log = logging.getLogger("server_economic.economic_data")

# ── 数据缓存 ──────────────────────────────────────────────────────────────

_data_cache: dict[str, dict] = {}   # {indicator: {value, unit, timestamp}}
_cache_ttl_seconds = 300           # 缓存有效期 5 分钟


def get_indicator(indicator: str) -> Optional[dict]:
    """
    获取单个经济指标的最新值

    Args:
        indicator: 指标名称（如 cpi, gdp, interest_rate）

    Returns:
        数据点字典或 None
    """
    cached = _data_cache.get(indicator)
    if cached and (time.time() - cached["_cached_at"] < _cache_ttl_seconds):
        return cached

    # 采集新数据（模拟）
    new_data = _fetch_indicator(indicator)
    if new_data:
        _data_cache[indicator] = {**new_data, "_cached_at": time.time()}
        log.debug(f"Fetched fresh data for {indicator}: {new_data['value']}")
    return new_data or cached or None


def get_all_indicators() -> dict[str, dict]:
    """获取所有支持指标的最新快照"""
    result = {}
    for cap in _SUPPORTED_INDICATORS:
        data = get_indicator(cap)
        if data:
            result[cap] = data
    return result


# ── 内部数据源（模拟）───────────────────────────────────────────────────

_SUPPORTED_INDICATORS = [
    "cpi", "gdp", "interest_rate", "employment", "trade_balance"
]

_BASE_VALUES = {
    "cpi":           {"base": 310.5, "unit": "Index", "source": "BLS"},
    "gdp":           {"base": 28780.0, "unit": "Billion USD", "source": "BEA"},
    "interest_rate": {"base": 5.25, "unit": "%", "source": "FED"},
    "employment":    {"base": 158.5, "unit": "Million", "source": "BLS"},
    "trade_balance": {"base": -72.3, "unit": "Billion USD", "source": "Census"},
}


def _fetch_indicator(indicator: str) -> Optional[dict]:
    """
    模拟采集经济指标数据

    在实际部署中，此处替换为：
      - 调用美联储/FRED/BLS API
      - 查询内部数据库
      - 从消息队列消费实时数据

    Args:
        indicator: 指标名

    Returns:
        数据点字典或 None
    """
    base_info = _BASE_VALUES.get(indicator)
    if not base_info:
        log.warning(f"Unsupported indicator: {indicator}")
        return None

    # 模拟小幅波动（±0.5% ~ ±2%）
    base = base_info["base"]
    volatility = abs(base) * random.uniform(0.002, 0.02)
    value = base + random.uniform(-volatility, volatility)

    # 四舍五入到合理精度
    if indicator == "cpi":
        value = round(value, 1)
    elif indicator == "gdp":
        value = round(value, 1)
    elif indicator == "interest_rate":
        value = round(value * 4) / 4  # 25bp 步进
    elif indicator == "employment":
        value = round(value, 2)
    else:
        value = round(value, 1)

    now = datetime.now(timezone.utc)
    period = now.strftime("%b-%Y")

    return {
        "indicator": indicator,
        "value": value,
        "unit": base_info["unit"],
        "period": period,
        "source": base_info["source"],
        "region": state.region if state.region else "US",
        "timestamp": now.isoformat(),
        "collected_at": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


# ── 数据摘要报告 ────────────────────────────────────────────────────────

def generate_summary_report() -> str:
    """
    生成经济数据摘要报告文本

    Returns:
        格式化的报告字符串
    """
    all_data = get_all_indicators()

    lines = [
        "=" * 50,
        f" Economic Data Summary — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "-" * 50,
    ]

    for indicator in _SUPPORTED_INDICATORS:
        d = all_data.get(indicator)
        if d:
            sign = "+" if isinstance(d["value"], (int, float)) and d["value"] >= 0 else ""
            lines.append(
                f"  [{indicator.upper():>14}] "
                f"{sign}{d['value']:>10} {d['unit']:<18} "
                f"| {d['period']} | {d['source']}"
            )
        else:
            lines.append(f"  [{indicator.upper():>14}] {'N/A':>30}")

    lines.extend([
        "-" * 50,
        f" Total indicators: {len(all_data)}",
        "=" * 50,
    ])
    return "\n".join(lines)
