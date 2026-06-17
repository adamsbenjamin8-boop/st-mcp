"""
ServiceTitan API client for st_connector.
Handles token refresh, pagination, and entity-specific fetch helpers.
"""

import os
import time
import logging
import httpx

from .config import ST_AUTH_URL, ST_API_BASE

log = logging.getLogger(__name__)

_token_cache: dict = {"token": None, "expires_at": 0.0}


def _get_token() -> str:
    now = time.monotonic()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 30:
        return _token_cache["token"]
    resp = httpx.post(ST_AUTH_URL, data={
        "grant_type":    "client_credentials",
        "client_id":     os.environ["ST_CLIENT_ID"],
        "client_secret": os.environ["ST_CLIENT_SECRET"],
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"]      = data["access_token"]
    _token_cache["expires_at"] = now + data.get("expires_in", 3600)
    log.debug("ST token refreshed")
    return _token_cache["token"]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "ST-App-Key":    os.environ["ST_APP_KEY"],
        "Content-Type":  "application/json",
    }


def _tenant_url(api: str, path: str) -> str:
    return f"{ST_API_BASE}/{api}/v2/tenant/{os.environ['ST_TENANT_ID']}/{path}"


def get(api: str, path: str, params: dict = None) -> dict:
    """Single-page GET."""
    r = httpx.get(_tenant_url(api, path), headers=_headers(), params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def patch(api: str, path: str, payload: dict) -> dict:
    """Single-item PATCH — returns response body (empty dict if 204)."""
    r = httpx.patch(_tenant_url(api, path), headers=_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json() if r.content else {}


def get_all(api: str, path: str, params: dict = None, page_size: int = 500,
            progress_cb=None) -> list:
    """Paginated GET — returns all items across all pages.

    progress_cb(fetched: int, total: int | None) is called after each page.
    """
    p = {**(params or {}), "page": 1, "pageSize": page_size}
    results = []
    total = None
    while True:
        data = get(api, path, p)
        items = data.get("data", [])
        results.extend(items)
        if total is None:
            total = data.get("totalCount")
        if progress_cb:
            progress_cb(len(results), total)
        if not data.get("hasMore", False):
            break
        p["page"] += 1
    return results


# ---------------------------------------------------------------------------
# Entity-specific fetchers
# ---------------------------------------------------------------------------

def fetch_appointments(extra_params: dict = None, modified_after: str = None,
                       progress_cb=None) -> list:
    params = {**(extra_params or {})}
    if modified_after:
        params["modifiedOnOrAfter"] = modified_after
    return get_all("jpm", "appointments", params, progress_cb=progress_cb)


def fetch_jobs(extra_params: dict = None, modified_after: str = None,
               progress_cb=None) -> list:
    params = {**(extra_params or {})}
    if modified_after:
        params["modifiedOnOrAfter"] = modified_after
    return get_all("jpm", "jobs", params, progress_cb=progress_cb)


def fetch_invoices(extra_params: dict = None, modified_after: str = None,
                   progress_cb=None) -> list:
    params = {**(extra_params or {})}
    if modified_after:
        params["modifiedOnOrAfter"] = modified_after
    return get_all("accounting", "invoices", params, progress_cb=progress_cb)


def fetch_tasks(extra_params: dict = None, modified_after: str = None,
                progress_cb=None) -> list:
    params = {**(extra_params or {})}
    if modified_after:
        params["modifiedOnOrAfter"] = modified_after
    return get_all("taskmanagement", "tasks", params, progress_cb=progress_cb)


def fetch_estimates(extra_params: dict = None, modified_after: str = None,
                    progress_cb=None) -> list:
    params = {**(extra_params or {})}
    if modified_after:
        params["modifiedOnOrAfter"] = modified_after
    return get_all("sales", "estimates", params, progress_cb=progress_cb)


def fetch_purchase_orders(extra_params: dict = None, modified_after: str = None,
                          progress_cb=None) -> list:
    params = {**(extra_params or {})}
    if modified_after:
        params["modifiedOnOrAfter"] = modified_after
    return get_all("inventory", "purchase-orders", params, progress_cb=progress_cb)


def get_job(job_id: int) -> dict:
    try:
        return get("jpm", f"jobs/{job_id}")
    except Exception:
        return {}


def fetch_jobs_by_ids(job_ids: list[int]) -> dict[int, dict]:
    """Fetch a batch of jobs by ID in one API call. Returns {jobId: job_dict}."""
    if not job_ids:
        return {}
    params = {"ids": ",".join(str(i) for i in job_ids), "pageSize": 500}
    jobs = get_all("jpm", "jobs", params)
    return {j["id"]: j for j in jobs}


def fetch_appointments_by_ids(appt_ids: list[int]) -> dict[int, dict]:
    """Fetch appointments by ID in one API call. Returns {apptId: appt_dict}."""
    if not appt_ids:
        return {}
    params = {"ids": ",".join(str(i) for i in appt_ids), "pageSize": 500}
    appts = get_all("jpm", "appointments", params)
    return {a["id"]: a for a in appts}


def get_invoice(invoice_id: int) -> dict:
    try:
        return get("accounting", f"invoices/{invoice_id}")
    except Exception:
        return {}
