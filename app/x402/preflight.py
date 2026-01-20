# app/x402/preflight.py
"""
Pre-flight balance checks for x402 payment operations.

This module verifies that the gateway has sufficient resources
before accepting payment requests:
- xBZZ balance on Gnosis (for stamp purchases)
- xDAI balance on Gnosis (for gas)
- Chequebook balance (for bandwidth)

Balance thresholds are configured via environment variables in app/core/config.py.
"""
import logging
from typing import Dict, Any, List

from app.core.config import settings
from app.services.swarm_api import get_wallet_info, get_chequebook_balance

logger = logging.getLogger(__name__)

# Conversion constants
PLUR_PER_BZZ = 10 ** 16  # 1 BZZ = 10^16 PLUR
WEI_PER_XDAI = 10 ** 18  # 1 xDAI = 10^18 wei


def plur_to_bzz(plur: int) -> float:
    """Convert PLUR to BZZ."""
    return plur / PLUR_PER_BZZ


def wei_to_xdai(wei: int) -> float:
    """Convert wei to xDAI."""
    return wei / WEI_PER_XDAI


def check_xbzz_balance() -> Dict[str, Any]:
    """
    Check xBZZ balance against configured threshold.

    Returns:
        Dict containing:
        - ok: bool - whether balance is above threshold
        - balance_plur: int - raw balance in PLUR
        - balance_bzz: float - balance in BZZ
        - threshold_bzz: float - warning threshold in BZZ
        - warning: str or None - warning message if below threshold
    """
    try:
        wallet_info = get_wallet_info()
        balance_plur = int(wallet_info.get("bzzBalance", 0))
        balance_bzz = plur_to_bzz(balance_plur)
        threshold_bzz = settings.X402_XBZZ_WARN_THRESHOLD

        warning = None
        ok = balance_bzz >= threshold_bzz

        if not ok:
            warning = (
                f"xBZZ balance ({balance_bzz:.4f} BZZ) is below threshold "
                f"({threshold_bzz} BZZ). Top up your Gnosis wallet."
            )
            logger.warning(f"Pre-flight check: {warning}")

        return {
            "ok": ok,
            "balance_plur": balance_plur,
            "balance_bzz": balance_bzz,
            "threshold_bzz": threshold_bzz,
            "warning": warning
        }

    except Exception as e:
        logger.error(f"Failed to check xBZZ balance: {e}")
        return {
            "ok": False,
            "balance_plur": 0,
            "balance_bzz": 0.0,
            "threshold_bzz": settings.X402_XBZZ_WARN_THRESHOLD,
            "warning": f"Failed to fetch xBZZ balance: {str(e)}"
        }


def check_xdai_balance() -> Dict[str, Any]:
    """
    Check xDAI (native token) balance against configured threshold.

    The Bee /wallet endpoint returns nativeTokenBalance which is xDAI on Gnosis chain.

    Returns:
        Dict containing:
        - ok: bool - whether balance is above threshold
        - balance_wei: int - raw balance in wei
        - balance_xdai: float - balance in xDAI
        - threshold_xdai: float - warning threshold in xDAI
        - warning: str or None - warning message if below threshold
    """
    try:
        wallet_info = get_wallet_info()
        balance_wei = int(wallet_info.get("nativeTokenBalance", 0))
        balance_xdai = wei_to_xdai(balance_wei)
        threshold_xdai = settings.X402_XDAI_WARN_THRESHOLD

        warning = None
        ok = balance_xdai >= threshold_xdai

        if not ok:
            warning = (
                f"xDAI balance ({balance_xdai:.4f} xDAI) is below threshold "
                f"({threshold_xdai} xDAI). Top up your Gnosis wallet for gas."
            )
            logger.warning(f"Pre-flight check: {warning}")

        return {
            "ok": ok,
            "balance_wei": balance_wei,
            "balance_xdai": balance_xdai,
            "threshold_xdai": threshold_xdai,
            "warning": warning
        }

    except Exception as e:
        logger.error(f"Failed to check xDAI balance: {e}")
        return {
            "ok": False,
            "balance_wei": 0,
            "balance_xdai": 0.0,
            "threshold_xdai": settings.X402_XDAI_WARN_THRESHOLD,
            "warning": f"Failed to fetch xDAI balance: {str(e)}"
        }


def check_chequebook_balance() -> Dict[str, Any]:
    """
    Check chequebook balance against configured threshold.

    The chequebook holds xBZZ for bandwidth payments.

    Returns:
        Dict containing:
        - ok: bool - whether balance is above threshold
        - available_balance_plur: int - available balance in PLUR
        - available_balance_bzz: float - available balance in BZZ
        - total_balance_plur: int - total balance in PLUR
        - total_balance_bzz: float - total balance in BZZ
        - threshold_bzz: float - warning threshold in BZZ
        - warning: str or None - warning message if below threshold
    """
    try:
        chequebook_info = get_chequebook_balance()
        available_plur = int(chequebook_info.get("availableBalance", 0))
        total_plur = int(chequebook_info.get("totalBalance", 0))
        available_bzz = plur_to_bzz(available_plur)
        total_bzz = plur_to_bzz(total_plur)
        threshold_bzz = settings.X402_CHEQUEBOOK_WARN_THRESHOLD

        warning = None
        ok = available_bzz >= threshold_bzz

        if not ok:
            warning = (
                f"Chequebook available balance ({available_bzz:.4f} BZZ) is below threshold "
                f"({threshold_bzz} BZZ). Top up your chequebook for bandwidth payments."
            )
            logger.warning(f"Pre-flight check: {warning}")

        return {
            "ok": ok,
            "available_balance_plur": available_plur,
            "available_balance_bzz": available_bzz,
            "total_balance_plur": total_plur,
            "total_balance_bzz": total_bzz,
            "threshold_bzz": threshold_bzz,
            "warning": warning
        }

    except Exception as e:
        logger.error(f"Failed to check chequebook balance: {e}")
        return {
            "ok": False,
            "available_balance_plur": 0,
            "available_balance_bzz": 0.0,
            "total_balance_plur": 0,
            "total_balance_bzz": 0.0,
            "threshold_bzz": settings.X402_CHEQUEBOOK_WARN_THRESHOLD,
            "warning": f"Failed to fetch chequebook balance: {str(e)}"
        }


def check_preflight_balances() -> Dict[str, Any]:
    """
    Check all gateway balances and return pass/fail status.

    This is the main entry point for pre-flight checks before accepting
    x402 payment requests.

    Returns:
        Dict containing:
        - can_accept: bool - whether gateway can accept new payments
        - xbzz_ok: bool - xBZZ balance above threshold
        - xdai_ok: bool - xDAI balance above threshold
        - chequebook_ok: bool - chequebook balance above threshold
        - balances: dict - current balance values
        - warnings: list - non-blocking warnings
        - errors: list - blocking errors

    The gateway can still accept payments if there are warnings,
    but not if there are errors.
    """
    warnings: List[str] = []
    errors: List[str] = []

    # Check all balances
    xbzz_result = check_xbzz_balance()
    xdai_result = check_xdai_balance()
    chequebook_result = check_chequebook_balance()

    # Collect warnings from each check
    if xbzz_result.get("warning"):
        warnings.append(xbzz_result["warning"])
    if xdai_result.get("warning"):
        warnings.append(xdai_result["warning"])
    if chequebook_result.get("warning"):
        warnings.append(chequebook_result["warning"])

    # Determine if we can accept payments
    # All checks must pass to accept payments
    xbzz_ok = xbzz_result["ok"]
    xdai_ok = xdai_result["ok"]
    chequebook_ok = chequebook_result["ok"]

    # Build error messages for complete failures (e.g., API errors)
    if xbzz_result["balance_bzz"] == 0.0 and "Failed to fetch" in str(xbzz_result.get("warning", "")):
        errors.append("Cannot verify xBZZ balance - Bee node may be unreachable")
    if xdai_result["balance_xdai"] == 0.0 and "Failed to fetch" in str(xdai_result.get("warning", "")):
        errors.append("Cannot verify xDAI balance - Bee node may be unreachable")
    if chequebook_result["available_balance_bzz"] == 0.0 and "Failed to fetch" in str(chequebook_result.get("warning", "")):
        errors.append("Cannot verify chequebook balance - Bee node may be unreachable")

    # Can accept if all checks pass OR if they fail only due to low balance (not API errors)
    # Low balance is a warning, not a blocking error for now
    can_accept = len(errors) == 0

    logger.info(
        f"Pre-flight check complete: can_accept={can_accept}, "
        f"xbzz_ok={xbzz_ok}, xdai_ok={xdai_ok}, chequebook_ok={chequebook_ok}, "
        f"warnings={len(warnings)}, errors={len(errors)}"
    )

    return {
        "can_accept": can_accept,
        "xbzz_ok": xbzz_ok,
        "xdai_ok": xdai_ok,
        "chequebook_ok": chequebook_ok,
        "balances": {
            "xbzz": {
                "balance_bzz": xbzz_result["balance_bzz"],
                "threshold_bzz": xbzz_result["threshold_bzz"]
            },
            "xdai": {
                "balance_xdai": xdai_result["balance_xdai"],
                "threshold_xdai": xdai_result["threshold_xdai"]
            },
            "chequebook": {
                "available_bzz": chequebook_result["available_balance_bzz"],
                "total_bzz": chequebook_result["total_balance_bzz"],
                "threshold_bzz": chequebook_result["threshold_bzz"]
            }
        },
        "warnings": warnings,
        "errors": errors
    }
