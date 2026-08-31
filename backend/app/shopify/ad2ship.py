"""ad2ship public courier-tracking page parser.

Fetches and parses ad2ship's public ``/track-order/<awb>`` HTML page using the
stdlib ``re`` module only (no HTML-parser dependency). Used by the delivery
sweep job and the order-tracking agent to tell a genuine customer delivery
apart from a return-to-origin (RTO).

``fetch_tracking`` never raises: any transport error, non-200 response, page
without a recognised status badge, or parse failure yields ``None``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_TRACK_URL = "https://ad2ship.com/track-order/{awb}"
_HEADERS = {"User-Agent": "Mozilla/5.0"}

_BADGE_RE = re.compile(r'class="status-badge ([a-z_]+)"')
_BADGE_LABEL_RE = re.compile(
    r'class="status-badge [a-z_]+">\s*(?:<i[^>]*></i>\s*)?([^<]+)'
)
_HISTORY_ITEM_MARKER = '<div class="history-item">'
_LAST_SCAN_RE = re.compile(
    r'<div class="h-status"><strong>(?:<i[^>]*></i>\s*)?([^<]+)</strong>'
)
_REMARK_RE = re.compile(r'<div class="h-remarks">([^<]*)</div>')
_LOCATION_RE = re.compile(
    r'<div class="h-location">(?:<i[^>]*></i>\s*)?([^<]*)'
)
_LOCATION_CITY_RE = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*$")
_SCAN_AT_RE = re.compile(r'<span class="h-time">([^<]*)</span>')
_DATE_BOX_RE = re.compile(
    r'<div class="date-box"><span>([^<]*)</span><strong>([^<]*)</strong>'
)
_EXPECTED_LABEL_RE = re.compile(r"expected|estimated", re.IGNORECASE)


@dataclass(frozen=True)
class Ad2shipTracking:
    """A parsed snapshot of an ad2ship tracking page."""

    status: str
    status_label: str
    current_city: str | None
    current_hub: str | None
    last_scan: str | None
    last_scan_remark: str | None
    last_scan_at: str | None
    expected_date: str | None

    def is_delivered_to_customer(self) -> bool:
        """True only for a genuine customer delivery (not an RTO)."""
        return self.status == "delivered"

    def is_rto(self) -> bool:
        """True when the shipment is on/returned to origin (any ``rto*`` state)."""
        return self.status.startswith("rto")

    def is_terminal(self) -> bool:
        """True when no further movement is expected (delivered/rto*/cancelled)."""
        return (
            self.status == "delivered"
            or self.status.startswith("rto")
            or self.status == "cancelled"
        )


def _log_failure(exc: Exception) -> None:
    """Log a fetch/parse failure with the exception TYPE only (no awb/URL/message)."""
    logger.warning("ad2ship fetch failed: type=%s", type(exc).__name__)


def _first_history_item(html: str) -> str:
    """Return everything after the FIRST ``history-item`` marker.

    Caveat: this is the tail of the document, not a single isolated block — safe
    for the current `.search`-first-match callers, but a future caller that
    iterates history items must slice to the next marker itself.
    """
    parts = html.split(_HISTORY_ITEM_MARKER, 2)
    return parts[1] if len(parts) > 1 else ""


def _split_location(raw: str) -> tuple[str | None, str | None]:
    text = raw.strip()
    if not text:
        return None, None
    match = _LOCATION_CITY_RE.match(text)
    if match is None:
        return text, None
    hub = match.group(1).strip() or None
    city = match.group(2).strip() or None
    return hub, city


def _find_expected_date(html: str) -> str | None:
    for label, value in _DATE_BOX_RE.findall(html):
        if _EXPECTED_LABEL_RE.search(label):
            return value.strip() or None
    return None


def _parse(html: str) -> Ad2shipTracking | None:
    badge = _BADGE_RE.search(html)
    if badge is None:
        return None
    status = badge.group(1).lower()

    label_match = _BADGE_LABEL_RE.search(html)
    status_label = label_match.group(1).strip() if label_match else ""

    item = _first_history_item(html)

    scan_match = _LAST_SCAN_RE.search(item)
    last_scan = scan_match.group(1).strip() if scan_match else None

    remark_match = _REMARK_RE.search(item)
    last_scan_remark = remark_match.group(1).strip() if remark_match else None

    scan_at_match = _SCAN_AT_RE.search(item)
    last_scan_at = scan_at_match.group(1).strip() if scan_at_match else None

    location_match = _LOCATION_RE.search(item)
    if location_match is not None:
        current_hub, current_city = _split_location(location_match.group(1))
    else:
        current_hub, current_city = None, None

    expected_date = _find_expected_date(html)

    return Ad2shipTracking(
        status=status,
        status_label=status_label,
        current_city=current_city,
        current_hub=current_hub,
        last_scan=last_scan,
        last_scan_remark=last_scan_remark,
        last_scan_at=last_scan_at,
        expected_date=expected_date,
    )


async def fetch_tracking(
    http: httpx.AsyncClient, awb: str, *, timeout: float = 4.0
) -> Ad2shipTracking | None:
    """Fetch and parse the ad2ship tracking page for ``awb``.

    Returns ``None`` on any transport error, a non-200 response, a page without a
    recognised status badge, or a parse failure. Never raises. Failure logs carry
    only the exception type name, never the awb, URL, or exception text.
    """
    try:
        resp = await http.get(
            _TRACK_URL.format(awb=quote(awb, safe="")),
            headers=_HEADERS,
            timeout=timeout,
            follow_redirects=False,
        )
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        _log_failure(exc)
        return None

    if resp.status_code != 200:
        return None

    try:
        return _parse(resp.text)
    except Exception as exc:
        _log_failure(exc)
        return None
