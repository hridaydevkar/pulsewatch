"""
Tests for the pluggable check system (pulsewatch.checks).

All external I/O is mocked — no real network to AWS, no real DB, no HTTP — so
these run offline and deterministically. We patch pulsewatch.checks.httpx and
pulsewatch.checks.create_engine, the module-level handles the checkers use.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pulsewatch import checks
from pulsewatch.checks import (
    CheckResult,
    run_check,
    http_check,
    aws_status_check,
    database_check,
    custom_api_check,
)


def svc(**kw):
    """A minimal duck-typed Service for the checkers."""
    kw.setdefault("check_type", "http")
    kw.setdefault("dependency_kind", None)
    kw.setdefault("check_config", None)
    kw.setdefault("url", None)
    return SimpleNamespace(**kw)


def fake_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


# --- http -----------------------------------------------------------------

def test_http_check_success():
    with patch.object(checks.httpx, "get", return_value=fake_response(200)) as get:
        result = http_check(svc(url="https://ok.example"))
    get.assert_called_once()
    assert result.success is True
    assert result.status_code == 200


def test_http_check_bad_status():
    with patch.object(checks.httpx, "get", return_value=fake_response(503)):
        result = http_check(svc(url="https://bad.example"))
    assert result.success is False
    assert result.error == "HTTP 503"


def test_http_check_connection_error():
    with patch.object(checks.httpx, "get", side_effect=OSError("refused")):
        result = http_check(svc(url="https://down.example"))
    assert result.success is False
    assert "refused" in result.error


def test_http_check_no_url():
    assert http_check(svc(url=None)).success is False


# --- aws_status -----------------------------------------------------------

def _aws_event(region_code, service="ec2", status=1):
    return {
        "arn": f"arn:aws:health:{region_code}::event/EC2/AWS_EC2_ISSUE/x",
        "region_name": "N. Virginia",
        "status": status,
        "service": service,
        "service_name": f"Amazon {service.upper()}",
        "summary": "Increased Error Rates",
        "event_log": [{"summary": "Investigating", "message": f"Issue in {region_code}", "status": status, "timestamp": 1}],
    }


def _aws_svc(**cfg):
    return svc(check_type="dependency", dependency_kind="aws_status", check_config=cfg)


def test_aws_up_when_feed_empty():
    with patch.object(checks.httpx, "get", return_value=fake_response(200, [])):
        result = aws_status_check(_aws_svc(region="us-east-1"))
    assert result.success is True


def test_aws_down_when_configured_region_impacted():
    feed = [_aws_event("us-east-1", status=1)]
    with patch.object(checks.httpx, "get", return_value=fake_response(200, feed)):
        result = aws_status_check(_aws_svc(region="us-east-1"))
    assert result.success is False
    assert "us-east-1" in result.error


def test_aws_up_when_only_other_region_impacted():
    feed = [_aws_event("eu-west-1", status=1)]
    with patch.object(checks.httpx, "get", return_value=fake_response(200, feed)):
        result = aws_status_check(_aws_svc(region="us-east-1"))
    assert result.success is True


def test_aws_ignores_resolved_events():
    feed = [_aws_event("us-east-1", status=3)]  # 3 == resolved
    with patch.object(checks.httpx, "get", return_value=fake_response(200, feed)):
        result = aws_status_check(_aws_svc(region="us-east-1"))
    assert result.success is True


def test_aws_service_filter_excludes_unlisted_service():
    feed = [_aws_event("us-east-1", service="ec2", status=1)]
    with patch.object(checks.httpx, "get", return_value=fake_response(200, feed)):
        result = aws_status_check(_aws_svc(region="us-east-1", services=["S3"]))
    assert result.success is True  # only S3 watched, EC2 impacted


def test_aws_service_filter_matches_listed_service():
    feed = [_aws_event("us-east-1", service="ec2", status=1)]
    with patch.object(checks.httpx, "get", return_value=fake_response(200, feed)):
        result = aws_status_check(_aws_svc(region="us-east-1", services=["EC2"]))
    assert result.success is False


def test_aws_requires_region_or_services():
    result = aws_status_check(_aws_svc())
    assert result.success is False
    assert "region" in result.error


def test_aws_feed_http_error_is_down():
    with patch.object(checks.httpx, "get", return_value=fake_response(500)):
        result = aws_status_check(_aws_svc(region="us-east-1"))
    assert result.success is False
    assert "500" in result.error


def test_aws_normalizes_spaced_region_labels():
    # AWS sometimes formats regions as "us - east - 1" in the service field.
    event = {"arn": "", "service": "multiple services - us - east - 1",
             "service_name": "", "region_name": "", "summary": "", "status": 1}
    with patch.object(checks.httpx, "get", return_value=fake_response(200, [event])):
        result = aws_status_check(_aws_svc(region="us-east-1"))
    assert result.success is False


# --- database -------------------------------------------------------------

def _db_svc(conn="postgresql://u:p@host/db"):
    return svc(check_type="dependency", dependency_kind="database",
               check_config={"connection_string": conn})


def test_database_check_success():
    mock_engine = MagicMock()
    with patch.object(checks, "create_engine", return_value=mock_engine) as ce:
        result = database_check(_db_svc())
    ce.assert_called_once_with("postgresql://u:p@host/db")
    # SELECT 1 executed within the connection context
    conn = mock_engine.connect.return_value.__enter__.return_value
    conn.execute.assert_called_once()
    mock_engine.dispose.assert_called_once()
    assert result.success is True


def test_database_check_connection_failure():
    with patch.object(checks, "create_engine", side_effect=Exception("no route to host")):
        result = database_check(_db_svc())
    assert result.success is False
    assert "no route to host" in result.error


def test_database_check_query_failure_disposes_engine():
    mock_engine = MagicMock()
    conn = mock_engine.connect.return_value.__enter__.return_value
    conn.execute.side_effect = Exception("connection reset")
    with patch.object(checks, "create_engine", return_value=mock_engine):
        result = database_check(_db_svc())
    assert result.success is False
    assert "connection reset" in result.error
    mock_engine.dispose.assert_called_once()  # cleaned up even on failure


def test_database_check_no_connection_string():
    result = database_check(svc(check_type="dependency", dependency_kind="database",
                                check_config={}))
    assert result.success is False
    assert "connection_string" in result.error


# --- custom_api -----------------------------------------------------------

def _api_svc(url="https://status.stripe.com/api/v2/status.json",
             field="status.indicator", expected="none"):
    return svc(check_type="dependency", dependency_kind="custom_api", url=url,
               check_config={"json_field": field, "expected_value": expected})


def test_custom_api_field_matches_expected():
    body = {"status": {"indicator": "none", "description": "All Systems Operational"}}
    with patch.object(checks.httpx, "get", return_value=fake_response(200, body)):
        result = custom_api_check(_api_svc())
    assert result.success is True


def test_custom_api_field_mismatch_is_down():
    body = {"status": {"indicator": "major"}}
    with patch.object(checks.httpx, "get", return_value=fake_response(200, body)):
        result = custom_api_check(_api_svc())
    assert result.success is False
    assert "expected" in result.error and "major" in result.error


def test_custom_api_missing_field_is_down():
    body = {"status": {}}
    with patch.object(checks.httpx, "get", return_value=fake_response(200, body)):
        result = custom_api_check(_api_svc())
    assert result.success is False


def test_custom_api_http_error_is_down():
    with patch.object(checks.httpx, "get", return_value=fake_response(500)):
        result = custom_api_check(_api_svc())
    assert result.success is False
    assert "500" in result.error


def test_custom_api_no_field_is_status_check():
    with patch.object(checks.httpx, "get", return_value=fake_response(200, {"anything": 1})):
        result = custom_api_check(_api_svc(field=None, expected=None))
    assert result.success is True


def test_custom_api_no_url():
    result = custom_api_check(svc(check_type="dependency", dependency_kind="custom_api",
                                  check_config={}))
    assert result.success is False


# --- run_check dispatch ---------------------------------------------------

def test_run_check_defaults_to_http():
    with patch.object(checks.httpx, "get", return_value=fake_response(200)):
        result = run_check(svc(url="https://ok.example"))  # check_type "http"
    assert result.success is True


def test_run_check_unknown_dependency_kind():
    result = run_check(svc(check_type="dependency", dependency_kind="carrier_pigeon"))
    assert result.success is False
    assert "unknown dependency_kind" in result.error


def test_run_check_routes_to_dependency_checker():
    with patch.object(checks.httpx, "get", return_value=fake_response(200, [])):
        result = run_check(_aws_svc(region="us-east-1"))
    assert result.success is True
