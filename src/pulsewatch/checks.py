"""
checks.py
Pluggable check system. A "check" takes a Service and returns a CheckResult.

Two check types:
- "http"       (default): pings service.url, success on 2xx/3xx (unchanged behaviour)
- "dependency": runs a dependency_kind checker for an upstream provider —
                aws_status / database / custom_api — configured via check_config.

External I/O (httpx, DB engines) is referenced at module level so tests can
patch pulsewatch.checks.httpx / pulsewatch.checks.create_engine.
"""

from dataclasses import dataclass
from typing import Optional

import httpx
from sqlalchemy import create_engine, text

# The public AWS Health events feed. The old status.aws.amazon.com/data.json
# now 301-redirects here; it's a JSON array of *current* events (empty when all
# is well), each carrying an `arn` (contains the region code, e.g. us-east-1),
# service/service_name, a summary, and a numeric `status` (1/2 active, 3 resolved).
DEFAULT_AWS_FEED = "https://health.aws.amazon.com/public/currentevents"
# AWS event statuses that count as "impacting" (3 == resolved is excluded).
DEFAULT_AWS_IMPACTING_STATUSES = (1, 2)


@dataclass
class CheckResult:
    success: bool
    status_code: Optional[int] = None
    error: Optional[str] = None
    detail: Optional[str] = None


# --- http (the original behaviour) ----------------------------------------

def http_check(service) -> CheckResult:
    if not service.url:
        return CheckResult(False, error="no url configured")
    try:
        resp = httpx.get(service.url, timeout=10)
    except Exception as e:
        return CheckResult(False, error=str(e))
    ok = 200 <= resp.status_code < 400
    return CheckResult(
        ok,
        status_code=resp.status_code,
        error=None if ok else f"HTTP {resp.status_code}",
    )


# --- dependency: aws_status -----------------------------------------------

def _normalize(value) -> str:
    """Lowercase and strip spaces so region codes match across AWS's
    inconsistent formats ('us-east-1' in an arn vs 'us - east - 1' in a
    service label)."""
    return str(value).lower().replace(" ", "")


def _event_haystack(event: dict) -> str:
    parts = [event.get(k, "") for k in ("arn", "service", "service_name",
                                        "region_name", "summary")]
    for entry in event.get("event_log") or []:
        if isinstance(entry, dict):
            parts.append(entry.get("message", ""))
            parts.append(entry.get("summary", ""))
    return _normalize(" ".join(str(p) for p in parts))


def aws_status_check(service) -> CheckResult:
    cfg = service.check_config or {}
    region = cfg.get("region")
    services_filter = [str(s) for s in (cfg.get("services") or [])]
    if not region and not services_filter:
        return CheckResult(
            False,
            error="aws_status needs a 'region' (and optionally 'services') so it "
                  "reports on what you care about, not the whole AWS feed",
        )
    impacting = set(cfg.get("impacting_statuses") or DEFAULT_AWS_IMPACTING_STATUSES)
    feed_url = cfg.get("feed_url") or DEFAULT_AWS_FEED

    try:
        resp = httpx.get(feed_url, timeout=10, follow_redirects=True)
    except Exception as e:
        return CheckResult(False, error=f"could not reach AWS status feed: {e}")
    if resp.status_code >= 400:
        return CheckResult(False, status_code=resp.status_code,
                           error=f"AWS status feed HTTP {resp.status_code}")
    try:
        events = resp.json()
    except Exception as e:
        return CheckResult(False, status_code=resp.status_code,
                           error=f"AWS status feed returned invalid JSON: {e}")
    if not isinstance(events, list):
        events = []

    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            status = int(event.get("status"))
        except (TypeError, ValueError):
            status = None
        if status is not None and status not in impacting:
            continue  # e.g. a resolved event

        haystack = _event_haystack(event)
        if region and _normalize(region) not in haystack:
            continue
        if services_filter and not any(_normalize(s) in haystack for s in services_filter):
            continue

        summary = event.get("summary") or event.get("service_name") or "issue reported"
        scope = region or ", ".join(services_filter)
        return CheckResult(False, error=f"AWS {scope} impacted: {summary}", detail=summary)

    return CheckResult(True)


# --- dependency: database -------------------------------------------------

def database_check(service) -> CheckResult:
    cfg = service.check_config or {}
    conn_str = cfg.get("connection_string")
    if not conn_str:
        return CheckResult(False, error="database check has no 'connection_string'")
    engine = None
    try:
        engine = create_engine(conn_str)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return CheckResult(True, detail="SELECT 1 ok")
    except Exception as e:
        return CheckResult(False, error=f"DB connectivity failed: {e}")
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass


# --- dependency: custom_api -----------------------------------------------

def _dig(data, dotted_path: str):
    """Walk a dotted path ('status.indicator') through nested dicts."""
    current = data
    for part in str(dotted_path).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def custom_api_check(service) -> CheckResult:
    cfg = service.check_config or {}
    url = service.url or cfg.get("url")
    if not url:
        return CheckResult(False, error="custom_api check has no url")
    field = cfg.get("json_field")
    expected = cfg.get("expected_value")

    try:
        resp = httpx.get(url, timeout=10, follow_redirects=True)
    except Exception as e:
        return CheckResult(False, error=str(e))
    if resp.status_code >= 400:
        return CheckResult(False, status_code=resp.status_code,
                           error=f"HTTP {resp.status_code}")

    # No field configured -> behave like a plain status check.
    if not field:
        return CheckResult(True, status_code=resp.status_code)

    try:
        data = resp.json()
    except Exception as e:
        return CheckResult(False, status_code=resp.status_code,
                           error=f"invalid JSON: {e}")
    actual = _dig(data, field)
    if str(actual) == str(expected):
        return CheckResult(True, status_code=resp.status_code, detail=f"{field}={actual}")
    return CheckResult(False, status_code=resp.status_code,
                       error=f"{field}={actual!r}, expected {expected!r}")


DEPENDENCY_CHECKERS = {
    "aws_status": aws_status_check,
    "database": database_check,
    "custom_api": custom_api_check,
}


def run_check(service) -> CheckResult:
    """Dispatch to the right checker for a Service."""
    if (service.check_type or "http") == "dependency":
        checker = DEPENDENCY_CHECKERS.get(service.dependency_kind)
        if checker is None:
            return CheckResult(
                False,
                error=f"unknown dependency_kind: {service.dependency_kind!r} "
                      f"(supported: {', '.join(sorted(DEPENDENCY_CHECKERS))})",
            )
        return checker(service)
    return http_check(service)
