# app/x402/audit.py
"""
Audit logging for x402 transactions.

This module logs all x402 payment events for:
- Dispute resolution
- Financial reconciliation
- Debugging failures
- Compliance/audit

Log format: JSON lines (one event per line)
Log location: Configured via X402_AUDIT_LOG_PATH

Events logged:
- Request received (timestamp, client IP, endpoint, method)
- Preflight check (balances, pass/fail)
- Price calculated (BZZ cost, rate, markup, final price)
- 402 returned (price, currency, expiry)
- Payment received (payer, amount, currency, verification status)
- Payment settled (transaction hash, network)
- Stamp action (purchased/released, stampId)
- Error (type, context, recovery action)
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional
from enum import Enum

from app.core.config import settings

logger = logging.getLogger(__name__)


class AuditEventType(Enum):
    """Types of audit events that can be logged."""
    REQUEST_RECEIVED = "request_received"
    PREFLIGHT_CHECK = "preflight_check"
    PRICE_CALCULATED = "price_calculated"
    PAYMENT_REQUIRED_SENT = "payment_required_sent"
    PAYMENT_RECEIVED = "payment_received"
    PAYMENT_VERIFIED = "payment_verified"
    PAYMENT_SETTLED = "payment_settled"
    PAYMENT_FAILED = "payment_failed"
    ACCESS_BLOCKED = "access_blocked"
    ACCESS_WHITELISTED = "access_whitelisted"
    STAMP_PURCHASED = "stamp_purchased"
    STAMP_RELEASED = "stamp_released"
    DATA_UPLOADED = "data_uploaded"
    ERROR = "error"


def generate_request_id() -> str:
    """Generate a unique request ID for tracking."""
    return str(uuid.uuid4())[:8]


def get_audit_log_path() -> Path:
    """Get the path to the audit log file."""
    log_path = settings.X402_AUDIT_LOG_PATH
    return Path(log_path)


def ensure_audit_log_directory() -> bool:
    """
    Ensure the audit log directory exists.

    Returns:
        True if directory exists or was created, False on error
    """
    try:
        log_path = get_audit_log_path()
        log_dir = log_path.parent
        if not log_dir.exists():
            log_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created audit log directory: {log_dir}")
        return True
    except Exception as e:
        logger.error(f"Failed to create audit log directory: {e}")
        return False


def create_audit_event(
    event_type: AuditEventType,
    data: Dict[str, Any],
    client_ip: Optional[str] = None,
    wallet_address: Optional[str] = None,
    request_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create an audit event dictionary.

    Args:
        event_type: Type of event to log
        data: Event-specific data
        client_ip: Client IP address (if available)
        wallet_address: Client wallet address (if available)
        request_id: Unique request identifier (if available)

    Returns:
        A structured event ready to be written to the audit log
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type.value,
        "request_id": request_id or generate_request_id(),
        "client_ip": client_ip,
        "wallet_address": wallet_address,
        "data": data
    }


def log_audit_event(
    event_type: AuditEventType,
    data: Dict[str, Any],
    client_ip: Optional[str] = None,
    wallet_address: Optional[str] = None,
    request_id: Optional[str] = None
) -> Optional[str]:
    """
    Log an audit event to the x402 audit log.

    Args:
        event_type: Type of event to log
        data: Event-specific data
        client_ip: Client IP address (if available)
        wallet_address: Client wallet address (if available)
        request_id: Unique request identifier (if available)

    Returns:
        The request_id used for this event, or None on error
    """
    event = create_audit_event(
        event_type=event_type,
        data=data,
        client_ip=client_ip,
        wallet_address=wallet_address,
        request_id=request_id
    )

    try:
        # Ensure directory exists
        ensure_audit_log_directory()

        # Write event as JSON line
        log_path = get_audit_log_path()
        with open(log_path, "a") as f:
            f.write(json.dumps(event) + "\n")

        logger.debug(f"Audit event logged: {event_type.value} [{event['request_id']}]")
        return event["request_id"]

    except Exception as e:
        logger.error(f"Failed to write audit event: {e}")
        return None


# Convenience functions for specific event types

def log_request_received(
    client_ip: str,
    method: str,
    path: str,
    content_length: Optional[int] = None,
    request_id: Optional[str] = None
) -> Optional[str]:
    """Log a request received event."""
    return log_audit_event(
        event_type=AuditEventType.REQUEST_RECEIVED,
        data={
            "method": method,
            "path": path,
            "content_length": content_length,
        },
        client_ip=client_ip,
        request_id=request_id
    )


def log_preflight_check(
    client_ip: str,
    xbzz_ok: bool,
    xdai_ok: bool,
    chequebook_ok: bool,
    balances: Dict[str, Any],
    request_id: Optional[str] = None
) -> Optional[str]:
    """Log a preflight balance check event."""
    return log_audit_event(
        event_type=AuditEventType.PREFLIGHT_CHECK,
        data={
            "xbzz_ok": xbzz_ok,
            "xdai_ok": xdai_ok,
            "chequebook_ok": chequebook_ok,
            "balances": balances,
            "can_accept": xbzz_ok and xdai_ok and chequebook_ok,
        },
        client_ip=client_ip,
        request_id=request_id
    )


def log_price_calculated(
    client_ip: str,
    operation: str,
    price_usd: float,
    price_bzz: float,
    exchange_rate: float,
    markup_percent: float,
    request_id: Optional[str] = None
) -> Optional[str]:
    """Log a price calculation event."""
    return log_audit_event(
        event_type=AuditEventType.PRICE_CALCULATED,
        data={
            "operation": operation,
            "price_usd": price_usd,
            "price_bzz": price_bzz,
            "exchange_rate": exchange_rate,
            "markup_percent": markup_percent,
        },
        client_ip=client_ip,
        request_id=request_id
    )


def log_payment_required_sent(
    client_ip: str,
    price_usd: float,
    currency: str,
    network: str,
    pay_to: str,
    resource: str,
    request_id: Optional[str] = None
) -> Optional[str]:
    """Log a 402 Payment Required response event."""
    return log_audit_event(
        event_type=AuditEventType.PAYMENT_REQUIRED_SENT,
        data={
            "price_usd": price_usd,
            "currency": currency,
            "network": network,
            "pay_to": pay_to,
            "resource": resource,
        },
        client_ip=client_ip,
        request_id=request_id
    )


def log_payment_received(
    client_ip: str,
    payer: str,
    amount_usdc: str,
    network: str,
    request_id: Optional[str] = None
) -> Optional[str]:
    """Log a payment received event."""
    return log_audit_event(
        event_type=AuditEventType.PAYMENT_RECEIVED,
        data={
            "amount_usdc": amount_usdc,
            "network": network,
        },
        client_ip=client_ip,
        wallet_address=payer,
        request_id=request_id
    )


def log_payment_verified(
    client_ip: str,
    payer: str,
    is_valid: bool,
    invalid_reason: Optional[str] = None,
    request_id: Optional[str] = None
) -> Optional[str]:
    """Log a payment verification event."""
    return log_audit_event(
        event_type=AuditEventType.PAYMENT_VERIFIED,
        data={
            "is_valid": is_valid,
            "invalid_reason": invalid_reason,
        },
        client_ip=client_ip,
        wallet_address=payer,
        request_id=request_id
    )


def log_payment_settled(
    client_ip: str,
    payer: str,
    transaction_hash: Optional[str],
    network: str,
    success: bool,
    error_reason: Optional[str] = None,
    request_id: Optional[str] = None
) -> Optional[str]:
    """Log a payment settlement event."""
    return log_audit_event(
        event_type=AuditEventType.PAYMENT_SETTLED,
        data={
            "success": success,
            "transaction_hash": transaction_hash,
            "network": network,
            "error_reason": error_reason,
        },
        client_ip=client_ip,
        wallet_address=payer,
        request_id=request_id
    )


def log_payment_failed(
    client_ip: str,
    reason: str,
    stage: str,
    wallet_address: Optional[str] = None,
    request_id: Optional[str] = None
) -> Optional[str]:
    """Log a payment failure event."""
    return log_audit_event(
        event_type=AuditEventType.PAYMENT_FAILED,
        data={
            "reason": reason,
            "stage": stage,
        },
        client_ip=client_ip,
        wallet_address=wallet_address,
        request_id=request_id
    )


def log_access_blocked(
    client_ip: str,
    reason: str,
    request_id: Optional[str] = None
) -> Optional[str]:
    """Log an access blocked (blacklisted) event."""
    return log_audit_event(
        event_type=AuditEventType.ACCESS_BLOCKED,
        data={
            "reason": reason,
        },
        client_ip=client_ip,
        request_id=request_id
    )


def log_access_whitelisted(
    client_ip: str,
    request_id: Optional[str] = None
) -> Optional[str]:
    """Log an access granted via whitelist event."""
    return log_audit_event(
        event_type=AuditEventType.ACCESS_WHITELISTED,
        data={
            "payment_bypassed": True,
        },
        client_ip=client_ip,
        request_id=request_id
    )


def log_stamp_purchased(
    client_ip: str,
    stamp_id: str,
    amount: int,
    depth: int,
    duration_hours: int,
    cost_bzz: float,
    wallet_address: Optional[str] = None,
    request_id: Optional[str] = None
) -> Optional[str]:
    """Log a stamp purchase event."""
    return log_audit_event(
        event_type=AuditEventType.STAMP_PURCHASED,
        data={
            "stamp_id": stamp_id,
            "amount": amount,
            "depth": depth,
            "duration_hours": duration_hours,
            "cost_bzz": cost_bzz,
        },
        client_ip=client_ip,
        wallet_address=wallet_address,
        request_id=request_id
    )


def log_data_uploaded(
    client_ip: str,
    reference: str,
    size_bytes: int,
    stamp_id: str,
    wallet_address: Optional[str] = None,
    request_id: Optional[str] = None
) -> Optional[str]:
    """Log a data upload event."""
    return log_audit_event(
        event_type=AuditEventType.DATA_UPLOADED,
        data={
            "reference": reference,
            "size_bytes": size_bytes,
            "stamp_id": stamp_id,
        },
        client_ip=client_ip,
        wallet_address=wallet_address,
        request_id=request_id
    )


def log_error(
    client_ip: str,
    error_type: str,
    error_message: str,
    context: Optional[Dict[str, Any]] = None,
    wallet_address: Optional[str] = None,
    request_id: Optional[str] = None
) -> Optional[str]:
    """Log an error event."""
    return log_audit_event(
        event_type=AuditEventType.ERROR,
        data={
            "error_type": error_type,
            "error_message": error_message,
            "context": context or {},
        },
        client_ip=client_ip,
        wallet_address=wallet_address,
        request_id=request_id
    )


def read_audit_log(
    max_entries: int = 100,
    event_type: Optional[AuditEventType] = None,
    client_ip: Optional[str] = None
) -> list:
    """
    Read entries from the audit log.

    Args:
        max_entries: Maximum number of entries to return
        event_type: Filter by event type (optional)
        client_ip: Filter by client IP (optional)

    Returns:
        List of audit events (most recent first)
    """
    try:
        log_path = get_audit_log_path()
        if not log_path.exists():
            return []

        events = []
        with open(log_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    # Apply filters
                    if event_type and event.get("event_type") != event_type.value:
                        continue
                    if client_ip and event.get("client_ip") != client_ip:
                        continue
                    events.append(event)
                except json.JSONDecodeError:
                    continue

        # Return most recent first, limited to max_entries
        return list(reversed(events))[:max_entries]

    except Exception as e:
        logger.error(f"Failed to read audit log: {e}")
        return []


def get_audit_stats() -> Dict[str, Any]:
    """
    Get statistics from the audit log.

    Returns:
        Dict with event counts, date range, etc.
    """
    try:
        log_path = get_audit_log_path()
        if not log_path.exists():
            return {
                "total_events": 0,
                "events_by_type": {},
                "log_path": str(log_path),
                "log_exists": False,
            }

        events_by_type: Dict[str, int] = {}
        total = 0
        first_timestamp = None
        last_timestamp = None

        with open(log_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    total += 1
                    event_type = event.get("event_type", "unknown")
                    events_by_type[event_type] = events_by_type.get(event_type, 0) + 1

                    timestamp = event.get("timestamp")
                    if timestamp:
                        if first_timestamp is None:
                            first_timestamp = timestamp
                        last_timestamp = timestamp
                except json.JSONDecodeError:
                    continue

        return {
            "total_events": total,
            "events_by_type": events_by_type,
            "first_event": first_timestamp,
            "last_event": last_timestamp,
            "log_path": str(log_path),
            "log_exists": True,
        }

    except Exception as e:
        logger.error(f"Failed to get audit stats: {e}")
        return {
            "total_events": 0,
            "events_by_type": {},
            "log_path": str(get_audit_log_path()),
            "log_exists": False,
            "error": str(e),
        }
