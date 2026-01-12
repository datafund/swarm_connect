# tests/test_x402_audit.py
"""
Unit tests for x402 audit logging.
"""
import json
import os
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.x402.audit import (
    AuditEventType,
    generate_request_id,
    get_audit_log_path,
    ensure_audit_log_directory,
    create_audit_event,
    log_audit_event,
    log_request_received,
    log_preflight_check,
    log_price_calculated,
    log_payment_required_sent,
    log_payment_received,
    log_payment_verified,
    log_payment_settled,
    log_payment_failed,
    log_access_blocked,
    log_access_whitelisted,
    log_stamp_purchased,
    log_data_uploaded,
    log_error,
    read_audit_log,
    get_audit_stats,
)


class TestAuditEventType:
    """Test audit event type enumeration."""

    def test_event_types_exist(self):
        """All expected event types exist."""
        assert AuditEventType.REQUEST_RECEIVED.value == "request_received"
        assert AuditEventType.PREFLIGHT_CHECK.value == "preflight_check"
        assert AuditEventType.PRICE_CALCULATED.value == "price_calculated"
        assert AuditEventType.PAYMENT_REQUIRED_SENT.value == "payment_required_sent"
        assert AuditEventType.PAYMENT_RECEIVED.value == "payment_received"
        assert AuditEventType.PAYMENT_VERIFIED.value == "payment_verified"
        assert AuditEventType.PAYMENT_SETTLED.value == "payment_settled"
        assert AuditEventType.PAYMENT_FAILED.value == "payment_failed"
        assert AuditEventType.ACCESS_BLOCKED.value == "access_blocked"
        assert AuditEventType.ACCESS_WHITELISTED.value == "access_whitelisted"
        assert AuditEventType.STAMP_PURCHASED.value == "stamp_purchased"
        assert AuditEventType.DATA_UPLOADED.value == "data_uploaded"
        assert AuditEventType.ERROR.value == "error"


class TestGenerateRequestId:
    """Test request ID generation."""

    def test_generates_string(self):
        """Returns a string."""
        request_id = generate_request_id()
        assert isinstance(request_id, str)

    def test_correct_length(self):
        """Request ID has expected length."""
        request_id = generate_request_id()
        assert len(request_id) == 8

    def test_unique_ids(self):
        """Generated IDs are unique."""
        ids = [generate_request_id() for _ in range(100)]
        assert len(set(ids)) == 100


class TestCreateAuditEvent:
    """Test audit event creation."""

    def test_creates_event_structure(self):
        """Creates event with all required fields."""
        event = create_audit_event(
            event_type=AuditEventType.REQUEST_RECEIVED,
            data={"method": "POST", "path": "/api/v1/data/"},
            client_ip="192.168.1.1",
            wallet_address="0x1234",
            request_id="abc12345"
        )

        assert "timestamp" in event
        assert event["event_type"] == "request_received"
        assert event["request_id"] == "abc12345"
        assert event["client_ip"] == "192.168.1.1"
        assert event["wallet_address"] == "0x1234"
        assert event["data"]["method"] == "POST"
        assert event["data"]["path"] == "/api/v1/data/"

    def test_generates_request_id_if_not_provided(self):
        """Auto-generates request ID when not provided."""
        event = create_audit_event(
            event_type=AuditEventType.ERROR,
            data={"error": "test"},
            client_ip="192.168.1.1"
        )

        assert event["request_id"] is not None
        assert len(event["request_id"]) == 8

    def test_timestamp_is_iso_format(self):
        """Timestamp is in ISO format."""
        event = create_audit_event(
            event_type=AuditEventType.ERROR,
            data={},
            client_ip="192.168.1.1"
        )

        # Should be parseable as ISO format
        from datetime import datetime
        timestamp = event["timestamp"]
        assert "T" in timestamp
        assert timestamp.endswith("+00:00")


class TestLogAuditEvent:
    """Test audit event logging to file."""

    @patch("app.x402.audit.settings")
    def test_writes_event_to_file(self, mock_settings):
        """Events are written to the log file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            request_id = log_audit_event(
                event_type=AuditEventType.REQUEST_RECEIVED,
                data={"method": "POST"},
                client_ip="192.168.1.1"
            )

            assert request_id is not None
            assert log_path.exists()

            with open(log_path, "r") as f:
                content = f.read()
                assert "request_received" in content
                assert "192.168.1.1" in content

    @patch("app.x402.audit.settings")
    def test_writes_json_lines(self, mock_settings):
        """Events are written as JSON lines."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            # Log multiple events
            log_audit_event(AuditEventType.REQUEST_RECEIVED, {"test": 1}, "1.1.1.1")
            log_audit_event(AuditEventType.ERROR, {"test": 2}, "2.2.2.2")

            with open(log_path, "r") as f:
                lines = f.readlines()
                assert len(lines) == 2

                # Each line should be valid JSON
                for line in lines:
                    event = json.loads(line.strip())
                    assert "timestamp" in event
                    assert "event_type" in event

    @patch("app.x402.audit.settings")
    def test_creates_directory_if_missing(self, mock_settings):
        """Creates parent directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "subdir" / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_audit_event(AuditEventType.REQUEST_RECEIVED, {}, "1.1.1.1")

            assert log_path.exists()


class TestConvenienceLoggingFunctions:
    """Test convenience logging functions."""

    @patch("app.x402.audit.settings")
    def test_log_request_received(self, mock_settings):
        """Log request received event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            request_id = log_request_received(
                client_ip="192.168.1.1",
                method="POST",
                path="/api/v1/data/",
                content_length=1024
            )

            assert request_id is not None
            events = read_audit_log()
            assert len(events) == 1
            assert events[0]["event_type"] == "request_received"
            assert events[0]["data"]["method"] == "POST"
            assert events[0]["data"]["path"] == "/api/v1/data/"
            assert events[0]["data"]["content_length"] == 1024

    @patch("app.x402.audit.settings")
    def test_log_preflight_check(self, mock_settings):
        """Log preflight check event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_preflight_check(
                client_ip="192.168.1.1",
                xbzz_ok=True,
                xdai_ok=True,
                chequebook_ok=False,
                balances={"xbzz": 10.0, "xdai": 1.0}
            )

            events = read_audit_log()
            assert events[0]["event_type"] == "preflight_check"
            assert events[0]["data"]["can_accept"] is False  # chequebook_ok is False

    @patch("app.x402.audit.settings")
    def test_log_price_calculated(self, mock_settings):
        """Log price calculation event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_price_calculated(
                client_ip="192.168.1.1",
                operation="upload",
                price_usd=0.05,
                price_bzz=0.1,
                exchange_rate=0.5,
                markup_percent=50.0
            )

            events = read_audit_log()
            assert events[0]["event_type"] == "price_calculated"
            assert events[0]["data"]["price_usd"] == 0.05

    @patch("app.x402.audit.settings")
    def test_log_payment_required_sent(self, mock_settings):
        """Log 402 response event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_payment_required_sent(
                client_ip="192.168.1.1",
                price_usd=0.05,
                currency="USDC",
                network="base-sepolia",
                pay_to="0x1234",
                resource="https://gateway.example.com/api/v1/data/"
            )

            events = read_audit_log()
            assert events[0]["event_type"] == "payment_required_sent"
            assert events[0]["data"]["currency"] == "USDC"

    @patch("app.x402.audit.settings")
    def test_log_payment_verified(self, mock_settings):
        """Log payment verification event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_payment_verified(
                client_ip="192.168.1.1",
                payer="0xabc123",
                is_valid=True
            )

            events = read_audit_log()
            assert events[0]["event_type"] == "payment_verified"
            assert events[0]["wallet_address"] == "0xabc123"
            assert events[0]["data"]["is_valid"] is True

    @patch("app.x402.audit.settings")
    def test_log_payment_settled(self, mock_settings):
        """Log payment settlement event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_payment_settled(
                client_ip="192.168.1.1",
                payer="0xabc123",
                transaction_hash="0xdef456",
                network="base-sepolia",
                success=True
            )

            events = read_audit_log()
            assert events[0]["event_type"] == "payment_settled"
            assert events[0]["data"]["transaction_hash"] == "0xdef456"
            assert events[0]["data"]["success"] is True

    @patch("app.x402.audit.settings")
    def test_log_access_blocked(self, mock_settings):
        """Log access blocked event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_access_blocked(
                client_ip="192.168.1.100",
                reason="IP address is blocked"
            )

            events = read_audit_log()
            assert events[0]["event_type"] == "access_blocked"
            assert events[0]["data"]["reason"] == "IP address is blocked"

    @patch("app.x402.audit.settings")
    def test_log_access_whitelisted(self, mock_settings):
        """Log access whitelisted event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_access_whitelisted(client_ip="192.168.1.50")

            events = read_audit_log()
            assert events[0]["event_type"] == "access_whitelisted"
            assert events[0]["data"]["payment_bypassed"] is True

    @patch("app.x402.audit.settings")
    def test_log_stamp_purchased(self, mock_settings):
        """Log stamp purchase event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_stamp_purchased(
                client_ip="192.168.1.1",
                stamp_id="abc123",
                amount=1000000,
                depth=17,
                duration_hours=24,
                cost_bzz=0.5
            )

            events = read_audit_log()
            assert events[0]["event_type"] == "stamp_purchased"
            assert events[0]["data"]["stamp_id"] == "abc123"

    @patch("app.x402.audit.settings")
    def test_log_data_uploaded(self, mock_settings):
        """Log data upload event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_data_uploaded(
                client_ip="192.168.1.1",
                reference="abc123def456",
                size_bytes=1024,
                stamp_id="stamp123"
            )

            events = read_audit_log()
            assert events[0]["event_type"] == "data_uploaded"
            assert events[0]["data"]["reference"] == "abc123def456"

    @patch("app.x402.audit.settings")
    def test_log_error(self, mock_settings):
        """Log error event."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_error(
                client_ip="192.168.1.1",
                error_type="FacilitatorError",
                error_message="Connection refused",
                context={"endpoint": "/verify"}
            )

            events = read_audit_log()
            assert events[0]["event_type"] == "error"
            assert events[0]["data"]["error_type"] == "FacilitatorError"


class TestReadAuditLog:
    """Test reading from audit log."""

    @patch("app.x402.audit.settings")
    def test_read_returns_most_recent_first(self, mock_settings):
        """Events are returned most recent first."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_audit_event(AuditEventType.REQUEST_RECEIVED, {"order": 1}, "1.1.1.1")
            log_audit_event(AuditEventType.REQUEST_RECEIVED, {"order": 2}, "2.2.2.2")
            log_audit_event(AuditEventType.REQUEST_RECEIVED, {"order": 3}, "3.3.3.3")

            events = read_audit_log()
            assert events[0]["data"]["order"] == 3  # Most recent first
            assert events[2]["data"]["order"] == 1

    @patch("app.x402.audit.settings")
    def test_read_respects_max_entries(self, mock_settings):
        """Respects max_entries limit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            for i in range(10):
                log_audit_event(AuditEventType.REQUEST_RECEIVED, {"i": i}, "1.1.1.1")

            events = read_audit_log(max_entries=5)
            assert len(events) == 5

    @patch("app.x402.audit.settings")
    def test_read_filters_by_event_type(self, mock_settings):
        """Filters by event type."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_audit_event(AuditEventType.REQUEST_RECEIVED, {}, "1.1.1.1")
            log_audit_event(AuditEventType.ERROR, {}, "2.2.2.2")
            log_audit_event(AuditEventType.REQUEST_RECEIVED, {}, "3.3.3.3")

            events = read_audit_log(event_type=AuditEventType.REQUEST_RECEIVED)
            assert len(events) == 2
            assert all(e["event_type"] == "request_received" for e in events)

    @patch("app.x402.audit.settings")
    def test_read_filters_by_client_ip(self, mock_settings):
        """Filters by client IP."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_audit_event(AuditEventType.REQUEST_RECEIVED, {}, "1.1.1.1")
            log_audit_event(AuditEventType.REQUEST_RECEIVED, {}, "2.2.2.2")
            log_audit_event(AuditEventType.REQUEST_RECEIVED, {}, "1.1.1.1")

            events = read_audit_log(client_ip="1.1.1.1")
            assert len(events) == 2
            assert all(e["client_ip"] == "1.1.1.1" for e in events)

    @patch("app.x402.audit.settings")
    def test_read_returns_empty_for_nonexistent_file(self, mock_settings):
        """Returns empty list if log doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "nonexistent.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            events = read_audit_log()
            assert events == []


class TestGetAuditStats:
    """Test audit statistics."""

    @patch("app.x402.audit.settings")
    def test_stats_with_events(self, mock_settings):
        """Returns correct statistics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            log_audit_event(AuditEventType.REQUEST_RECEIVED, {}, "1.1.1.1")
            log_audit_event(AuditEventType.REQUEST_RECEIVED, {}, "2.2.2.2")
            log_audit_event(AuditEventType.ERROR, {}, "1.1.1.1")

            stats = get_audit_stats()

            assert stats["total_events"] == 3
            assert stats["events_by_type"]["request_received"] == 2
            assert stats["events_by_type"]["error"] == 1
            assert stats["log_exists"] is True
            assert stats["first_event"] is not None
            assert stats["last_event"] is not None

    @patch("app.x402.audit.settings")
    def test_stats_for_nonexistent_log(self, mock_settings):
        """Returns zero stats for nonexistent log."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "nonexistent.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            stats = get_audit_stats()

            assert stats["total_events"] == 0
            assert stats["events_by_type"] == {}
            assert stats["log_exists"] is False


class TestRequestIdTracking:
    """Test request ID tracking across events."""

    @patch("app.x402.audit.settings")
    def test_same_request_id_across_events(self, mock_settings):
        """Same request ID can be used across related events."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "audit.jsonl"
            mock_settings.X402_AUDIT_LOG_PATH = str(log_path)

            # Simulate a request flow with same request ID
            request_id = log_request_received("1.1.1.1", "POST", "/api/v1/data/")
            log_price_calculated("1.1.1.1", "upload", 0.05, 0.1, 0.5, 50.0, request_id)
            log_payment_required_sent(
                "1.1.1.1", 0.05, "USDC", "base-sepolia",
                "0x1234", "https://example.com/api/v1/data/", request_id
            )

            events = read_audit_log()
            assert len(events) == 3
            # All events should have same request_id
            assert all(e["request_id"] == request_id for e in events)
