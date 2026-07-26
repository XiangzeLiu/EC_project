from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from Server_manager.services import tastytrade_validation as validation


def _item(
    account_number: str,
    *,
    authority: str = "owner",
    closed: bool = False,
    nickname: str = "Primary",
) -> dict:
    return {
        "authority-level": authority,
        "account": {
            "account-number": account_number,
            "nickname": nickname,
            "account-type-name": "Individual",
            "is-closed": closed,
        },
    }


class TastytradeValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_account_selects_first_open_account(self):
        items = [_item("CLOSED", closed=True), _item("OPEN-1"), _item("OPEN-2")]
        with patch.object(validation, "_fetch_account_items", AsyncMock(return_value=items)):
            result = await validation.validate_tastytrade_credentials("secret", "token")

        self.assertEqual(result["selected_account"]["account_number"], "OPEN-1")
        self.assertEqual(len(result["accounts"]), 3)

    async def test_explicit_account_must_match_strictly(self):
        with patch.object(
            validation,
            "_fetch_account_items",
            AsyncMock(return_value=[_item("OPEN-1")]),
        ):
            with self.assertRaises(validation.TastytradeValidationError) as ctx:
                await validation.validate_tastytrade_credentials(
                    "secret",
                    "token",
                    account_number="MISSING",
                )

        self.assertEqual(ctx.exception.code, "TT_ACCOUNT_NOT_FOUND")

    async def test_closed_accounts_only_are_rejected_but_returned(self):
        with patch.object(
            validation,
            "_fetch_account_items",
            AsyncMock(return_value=[_item("CLOSED", closed=True)]),
        ):
            with self.assertRaises(validation.TastytradeValidationError) as ctx:
                await validation.validate_tastytrade_credentials("secret", "token")

        self.assertEqual(ctx.exception.code, "TT_NO_OPEN_ACCOUNTS")
        self.assertEqual(ctx.exception.accounts[0]["account_number"], "CLOSED")

    async def test_read_only_account_is_allowed_with_warning(self):
        with patch.object(
            validation,
            "_fetch_account_items",
            AsyncMock(return_value=[_item("READ-1", authority="read-only")]),
        ):
            result = await validation.validate_tastytrade_credentials("secret", "token")

        self.assertFalse(result["selected_account"]["can_trade"])
        self.assertTrue(result["warnings"])

    async def test_empty_account_list_is_rejected(self):
        with patch.object(validation, "_fetch_account_items", AsyncMock(return_value=[])):
            with self.assertRaises(validation.TastytradeValidationError) as ctx:
                await validation.validate_tastytrade_credentials("secret", "token")

        self.assertEqual(ctx.exception.code, "TT_NO_ACCOUNTS")

    async def test_invalid_oauth_pair_is_classified_without_echoing_credentials(self):
        with patch.object(
            validation,
            "_fetch_account_items",
            AsyncMock(side_effect=RuntimeError("401 invalid_grant")),
        ):
            with self.assertRaises(validation.TastytradeValidationError) as ctx:
                await validation.validate_tastytrade_credentials("private-secret", "private-token")

        self.assertEqual(ctx.exception.code, "TT_AUTH_INVALID")
        self.assertNotIn("private-secret", ctx.exception.message)
        self.assertNotIn("private-token", ctx.exception.message)

    async def test_network_timeout_is_retryable_user_feedback(self):
        with patch.object(
            validation,
            "_fetch_account_items",
            AsyncMock(side_effect=TimeoutError("timed out")),
        ):
            with self.assertRaises(validation.TastytradeValidationError) as ctx:
                await validation.validate_tastytrade_credentials("secret", "token")

        self.assertEqual(ctx.exception.code, "TT_NETWORK_TIMEOUT")


if __name__ == "__main__":
    unittest.main()
