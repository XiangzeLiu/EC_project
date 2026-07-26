from __future__ import annotations

"""Validate tastytrade OAuth credentials without persisting sensitive data."""

from dataclasses import dataclass
from typing import Any


@dataclass
class TastytradeValidationError(Exception):
    code: str
    message: str
    accounts: list[dict[str, Any]] | None = None

    def __str__(self) -> str:
        return self.message


def _account_summary(item: dict[str, Any]) -> dict[str, Any]:
    account = item.get("account") if isinstance(item.get("account"), dict) else item
    account_number = str(
        account.get("account-number") or account.get("account_number") or ""
    ).strip()
    authority = str(
        item.get("authority-level")
        or item.get("authority_level")
        or account.get("authority-level")
        or account.get("authority_level")
        or "unknown"
    ).strip().lower()
    is_closed = bool(account.get("is-closed", account.get("is_closed", False)))
    return {
        "account_number": account_number,
        "nickname": str(account.get("nickname") or "").strip(),
        "account_type": str(
            account.get("account-type-name") or account.get("account_type_name") or ""
        ).strip(),
        "authority_level": authority,
        "is_closed": is_closed,
        "can_trade": (not is_closed) and authority not in {"read-only", "read_only", "readonly"},
    }


async def _fetch_account_items(
    client_secret: str,
    refresh_token: str,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    try:
        from tastytrade import Session
    except ImportError as exc:
        raise TastytradeValidationError(
            "TT_SDK_MISSING",
            "SM 未安装兼容的 tastytrade SDK",
        ) from exc

    session = Session(client_secret, refresh_token, timeout=timeout_seconds)
    async with session as active_session:
        data = await active_session._get("/customers/me/accounts")
    items = data.get("items", []) if isinstance(data, dict) else []
    return [item for item in items if isinstance(item, dict)]


def _classify_exception(exc: Exception) -> TastytradeValidationError:
    text = str(exc or "").strip()
    lower = text.lower()
    if any(marker in lower for marker in (
        "invalid_grant",
        "invalid token",
        "invalid credentials",
        "unauthorized",
        "401",
    )):
        return TastytradeValidationError(
            "TT_AUTH_INVALID",
            "Client Secret 或 Refresh Token 无效或不匹配",
        )
    if "forbidden" in lower or "403" in lower:
        return TastytradeValidationError(
            "TT_AUTH_FORBIDDEN",
            "凭证有效，但没有访问账户信息的权限",
        )
    if "timeout" in lower or "timed out" in lower:
        return TastytradeValidationError(
            "TT_NETWORK_TIMEOUT",
            "连接 tastytrade 超时，请检查境外网络后重试",
        )
    return TastytradeValidationError(
        "TT_VALIDATION_FAILED",
        "tastytrade 凭证验证失败，请检查凭证和网络",
    )


async def validate_tastytrade_credentials(
    client_secret: str,
    refresh_token: str,
    account_number: str = "",
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    secret = str(client_secret or "").strip()
    token = str(refresh_token or "").strip()
    requested = str(account_number or "").strip()
    if not secret or not token:
        raise TastytradeValidationError(
            "TT_CREDENTIALS_REQUIRED",
            "Client Secret 和 Refresh Token 必须同时填写",
        )

    try:
        raw_items = await _fetch_account_items(secret, token, timeout_seconds)
    except TastytradeValidationError:
        raise
    except Exception as exc:
        raise _classify_exception(exc) from exc

    accounts = [summary for summary in map(_account_summary, raw_items) if summary["account_number"]]
    if not accounts:
        raise TastytradeValidationError(
            "TT_NO_ACCOUNTS",
            "凭证验证成功，但没有查询到任何账户",
            accounts=[],
        )

    open_accounts = [account for account in accounts if not account["is_closed"]]
    if not open_accounts:
        raise TastytradeValidationError(
            "TT_NO_OPEN_ACCOUNTS",
            "查询到的账户均已关闭，没有可用账户",
            accounts=accounts,
        )

    if requested:
        selected = next(
            (account for account in accounts if account["account_number"] == requested),
            None,
        )
        if selected is None:
            raise TastytradeValidationError(
                "TT_ACCOUNT_NOT_FOUND",
                f"Account Number {requested} 不在当前凭证可访问的账户中",
                accounts=accounts,
            )
        if selected["is_closed"]:
            raise TastytradeValidationError(
                "TT_ACCOUNT_CLOSED",
                f"Account Number {requested} 已关闭，不能作为 TS 运行账户",
                accounts=accounts,
            )
    else:
        selected = open_accounts[0]

    warnings: list[str] = []
    if not selected["can_trade"]:
        warnings.append("所选账户为 read-only，可查询但不能下单或撤单")

    return {
        "accounts": accounts,
        "selected_account": selected,
        "warnings": warnings,
    }
