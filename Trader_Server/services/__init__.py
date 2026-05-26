"""
Services sub-package for Server_economic
"""

# 方便外部直接 from services import xxx 的场景
from .registration import (
    test_connection,
    submit_registration,
    await_approval,
    run_full_registration,
    check_and_restore_session,
)
