#!/usr/bin/env python3
"""
ServiceTitan Writer MCP Connector
==================================
Handles write/update operations for four resources that the primary connector
lacks full coverage for:

  * Estimates       ->  JPM API v2           /jpm/v2/tenant/{tenant}/...
  * Invoices        ->  Accounting API v2    /accounting/v2/tenant/{tenant}/...
  * Purchase Orders ->  Inventory API v2     /inventory/v2/tenant/{tenant}/...
  * Timesheets      ->  Payroll API v2       /payroll/v2/tenant/{tenant}/...

Authentication
--------------
ServiceTitan uses OAuth2 Client Credentials + a per-app "ST-App-Key" header.
Set these environment variables before running:

  ST_CLIENT_ID      - OAuth2 client ID from your ST developer app
  ST_CLIENT_SECRET  - OAuth2 client secret
  ST_APP_KEY        - ST-App-Key value (app's unique key, not env-specific)
  ST_TENANT_ID      - Numeric tenant (account) ID
  ST_ENV            - "production" (default) or "sandbox"

Endpoints used
--------------
Confirmed against the ServiceTitan V2 API FAQ and developer portal docs.
Key notes:
  - PO status CANNOT be changed via API (create/update header fields only).
  - PO receipts are posted to /inventory/v2/.../receipts (top-level resource),
    not as a sub-path under purchase-orders.
  - If Flexible Timekeeping is enabled on the tenant, timesheet create/update
    endpoints return HTTP 400 from ServiceTitan -- check tenant settings first.
  - Timesheet approval is done entry-by-entry via individual PATCH calls.

Usage
-----
  pip install "mcp[cli]" httpx
  ST_CLIENT_ID=... ST_CLIENT_SECRET=... ST_APP_KEY=... ST_TENANT_ID=... \\
      python servicetitan_writer.py
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP

# -----------------------------------------------------------------------------
#  Environment & URL routing
# -----------------------------------------------------------------------------

_ST_ENV = os.getenv("ST_ENV", "production").lower()

if _ST_ENV == "sandbox":
    _AUTH_BASE = "https://auth-integration.servicetitan.io"
    _API_BASE  = "https://api-integration.servicetitan.io"
else:
    _AUTH_BASE = "https://auth.servicetitan.io"
    _API_BASE  = "https://api.servicetitan.io"

_TOKEN_URL = f"{_AUTH_BASE}/connect/token"


def _require_env(name: str) -> str:
    """Return env variable or raise with a clear message."""
    val = os.environ.get(name, "")
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set. "
            "Set ST_CLIENT_ID, ST_CLIENT_SECRET, ST_APP_KEY, and ST_TENANT_ID."
        )
    return val


# -----------------------------------------------------------------------------
#  OAuth2 token management
# -----------------------------------------------------------------------------

_token_cache: Dict[str, Any] = {"access_token": None, "expires_at": 0.0}


def _get_access_token() -> str:
    """Return a valid bearer token, refreshing only when near-expired (< 30 s left)."""
    if _token_cache["access_token"] and time.monotonic() < _token_cache["expires_at"] - 30:
        return str(_token_cache["access_token"])

    resp = httpx.post(
        _TOKEN_URL,
        data={
            "grant_type":    "client_credentials",
            "client_id":     _require_env("ST_CLIENT_ID"),
            "client_secret": _require_env("ST_CLIENT_SECRET"),
        },
        timeout=30,
    )
    # Raise immediately if auth fails -- a 4xx/5xx here means bad credentials.
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise EnvironmentError(
            f"ServiceTitan authentication failed ({exc.response.status_code}). "
            "Verify ST_CLIENT_ID and ST_CLIENT_SECRET are correct for the "
            f"'{_ST_ENV}' environment."
        ) from exc

    payload = resp.json()
    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"]   = time.monotonic() + int(payload.get("expires_in", 3600))
    return str(_token_cache["access_token"])


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_access_token()}",
        "ST-App-Key":    _require_env("ST_APP_KEY"),
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


# -----------------------------------------------------------------------------
#  HTTP helpers
# -----------------------------------------------------------------------------

def _url(api: str, path: str) -> str:
    """Build a fully-qualified ServiceTitan V2 tenant-scoped URL."""
    tenant = _require_env("ST_TENANT_ID")
    return f"{_API_BASE}/{api}/v2/tenant/{tenant}/{path}"


def _strip_none(d: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove keys whose value is None.

    IMPORTANT: this intentionally preserves falsy-but-meaningful values such
    as False, 0, 0.0, and "".  Only Python None is dropped so that PATCH
    requests don't accidentally null out fields the caller didn't mention.
    """
    return {k: v for k, v in d.items() if v is not None}


# ---------------------------------------------------------------------------
# Cache-first resolver
# ---------------------------------------------------------------------------

_CACHE_DB = "C:/ST/st_cache.db"

# Maps entity type → (table, name_columns, id_column, display_column)
_CACHE_TABLES: Dict[str, tuple] = {
    "vendors":             ("vendors",            ["name"],                   "id", "name"),
    "technicians":         ("technicians",         ["name"],                   "id", "name"),
    "business_units":      ("business_units",      ["name"],                   "id", "name"),
    "job_types":           ("job_types",           ["name"],                   "id", "name"),
    "customers":           ("customers",           ["name"],                   "id", "name"),
    "locations":           ("locations",           ["name", "address"],        "id", "name"),
    "po_types":            ("po_types",            ["name"],                   "id", "name"),
    "pricebook_services":  ("pricebook_services",  ["name", "code"],           "id", "name"),
    "pricebook_materials": ("pricebook_materials", ["name", "code"],           "id", "name"),
    "pricebook_equipment": ("pricebook_equipment", ["name", "code"],           "id", "name"),
    "inventory_locations": ("inventory_locations", ["name"],                   "id", "name"),
    "campaigns":           ("campaigns",           ["name"],                   "id", "name"),
    "tag_types":           ("tag_types",           ["name"],                   "id", "name"),
    "job_cancel_reasons":  ("job_cancel_reasons",  ["name"],                   "id", "name"),
    "membership_types":    ("membership_types",    ["name"],                   "id", "name"),
}


def _cache_resolve(entity_type: str, search: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Search the local SQLite cache for an entity by name/code.
    Returns a list of matching row dicts (may be empty on cache miss).

    This is called automatically at the start of any function that converts a
    human-readable name into a ServiceTitan ID — no separate cache_lookup call
    needed.  The caller falls back to a live API call only when this returns [].

    Args:
        entity_type: One of the keys in _CACHE_TABLES.
        search:      Partial name / code to search (case-insensitive LIKE).
        limit:       Maximum rows to return.
    """
    config = _CACHE_TABLES.get(entity_type)
    if not config or not os.path.exists(_CACHE_DB):
        return []

    table, name_cols, id_col, display_col = config
    try:
        import sqlite3
        conn = sqlite3.connect(_CACHE_DB)
        conn.row_factory = sqlite3.Row

        # Build WHERE clause: any name column LIKE %search%
        like = f"%{search.lower()}%"
        where = " OR ".join(f"LOWER({c}) LIKE ?" for c in name_cols)
        params = [like] * len(name_cols) + [limit]
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {where} LIMIT ?", params
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []   # cache miss is never fatal — always falls through to API


def _cache_resolve_id(entity_type: str, search: str) -> Optional[int]:
    """
    Convenience wrapper: returns just the ID of the best cache hit, or None.
    Best hit = exact name match first, then first partial match.
    """
    hits = _cache_resolve(entity_type, search, limit=20)
    if not hits:
        return None
    config = _CACHE_TABLES.get(entity_type)
    if not config:
        return None
    _, name_cols, id_col, _ = config
    # Prefer exact match on any name column
    s_lower = search.lower()
    for row in hits:
        for col in name_cols:
            if str(row.get(col, "")).lower() == s_lower:
                return row[id_col]
    # Fall back to first partial match
    return hits[0][id_col]


def _request(
    method: str,
    url: str,
    *,
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Execute an HTTP request and return a consistent dict:
      - On 204 No Content  -> {"success": True, "status_code": 204}
      - On 2xx with body   -> parsed JSON body
      - On 4xx/5xx         -> {"error": True, "status_code": N, "detail": ...}
      - On timeout/network -> {"error": True, "status_code": 0, "detail": "..."}
    """
    try:
        response = httpx.request(
            method,
            url,
            headers=_headers(),
            json=json,
            params=params,
            timeout=30,
        )
    except httpx.TimeoutException:
        return {"error": True, "status_code": 0, "detail": "Request timed out after 30 s."}
    except httpx.RequestError as exc:
        return {"error": True, "status_code": 0, "detail": f"Network error: {exc}"}

    # 204 No Content = success with no body (common for PATCH/DELETE in ST)
    if response.status_code == 204:
        return {"success": True, "status_code": 204}

    # Some 200/201 responses also have empty bodies — treat as success
    if not response.content:
        return {"success": True, "status_code": response.status_code}

    try:
        body = response.json()
    except Exception:
        body = response.text  # type: ignore[assignment]

    if response.status_code >= 400:
        # Surface helpful hint when ST says the endpoint isn't mapped
        detail = body
        if isinstance(body, str) and "Unable to match" in body:
            detail = (
                f"{body} — This usually means the ServiceTitan app is missing a required "
                "API scope. In the ST Developer portal, ensure the app has write scopes "
                "enabled (e.g. jpm:write, accounting:write, payroll:write, inventory:write)."
            )
        elif isinstance(body, dict) and "Unable to match" in str(body):
            detail = {**body, "_hint": (
                "This usually means a missing write API scope on the ST Developer app. "
                "Enable jpm:write, accounting:write, payroll:write, inventory:write in the portal."
            )}
        return {
            "error":       True,
            "status_code": response.status_code,
            "detail":      detail,
        }

    return body  # type: ignore[return-value]


# -----------------------------------------------------------------------------
#  MCP server
mcp = FastMCP("servicetitan-writer")


# =============================================================================
#  ESTIMATES  (JPM API v2)
#  Base path: /jpm/v2/tenant/{tenant}/estimates
# =============================================================================

@mcp.tool()
def estimate_update(
    estimate_id: int,
    name: str,
    summary: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Update top-level header fields on an existing estimate.
    PUT /sales/v2/tenant/{tenant}/estimates/{id}

    NOTE: This is a full PUT replace — name is REQUIRED even if you only want to
    change summary. Fetch the current estimate first to get the existing name.

    Args:
        estimate_id: ID of the estimate.
        name:        Display name (REQUIRED - pass existing name if unchanged).
        summary:     Free-text notes or summary.
    """
    body = _strip_none({"name": name, "summary": summary})
    return _request("PUT", _url("sales", f"estimates/{estimate_id}"), json=body)


@mcp.tool()
def estimate_add_item(
    estimate_id: int,
    sku_id: int,
    quantity: float = 1.0,
    sku_name: Optional[str] = None,
    description: Optional[str] = None,
    unit_price: Optional[float] = None,
    unit_cost: Optional[float] = None,
    is_add_on: Optional[bool] = None,
    item_group_name: Optional[str] = None,
    item_group_root_id: Optional[int] = None,
    technician_id: Optional[int] = None,
    order: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Append a line item (service, material, or equipment) to an estimate.
    PUT /sales/v2/tenant/{tenant}/estimates/{id}/items

    Args:
        estimate_id:        ID of the target estimate.
        sku_id:             Pricebook SKU ID. Required.
        quantity:           Number of units (default 1).
        sku_name:           Display name override (uses pricebook name if omitted).
        description:        Line-item description override.
        unit_price:         Price per unit (overrides pricebook price).
        unit_cost:          Cost per unit for margin tracking.
        is_add_on:          Mark as an add-on item.
        item_group_name:    Logical group name this item belongs to.
        item_group_root_id: Root item ID of the group (for grouped pricing).
        technician_id:      Technician credited with selling this item.
        order:              Display sequence position within the estimate.
    """
    body = _strip_none({
        "skuId":           sku_id,
        "quantity":        quantity,
        "skuName":         sku_name,
        "description":     description,
        "unitRate":        unit_price,   # ST estimates use unitRate, not unitPrice
        "unitCost":        unit_cost,
        "isAddOn":         is_add_on,
        "itemGroupName":   item_group_name,
        "itemGroupRootId": item_group_root_id,
        "employeeId":      technician_id,
        "order":           order,
    })
    return _request("PUT", _url("sales", f"estimates/{estimate_id}/items"), json=body)


@mcp.tool()
def estimate_update_item(
    estimate_id: int,
    item_id: int,
    sku_id: Optional[int] = None,
    sku_name: Optional[str] = None,
    description: Optional[str] = None,
    unit_price: Optional[float] = None,
    quantity: Optional[float] = None,
    unit_cost: Optional[float] = None,
    is_add_on: Optional[bool] = None,
    item_group_name: Optional[str] = None,
    item_group_root_id: Optional[int] = None,
    technician_id: Optional[int] = None,
    order: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Modify an existing line item on an estimate.
    PUT /sales/v2/tenant/{tenant}/estimates/{id}/items/{itemId}

    Args:
        estimate_id:        ID of the estimate.
        item_id:            ID of the line item to update.
        sku_id:             Replace with a different pricebook SKU.
        sku_name:           New display name.
        description:        New description.
        unit_price:         New price per unit.
        quantity:           New quantity.
        unit_cost:          New cost per unit.
        is_add_on:          Toggle add-on flag.
        item_group_name:    New item group name.
        item_group_root_id: New item group root ID.
        technician_id:      Updated selling technician.
        order:              New display order position.
    """
    body = _strip_none({
        "skuId":           sku_id,
        "skuName":         sku_name,
        "description":     description,
        "unitPrice":       unit_price,
        "quantity":        quantity,
        "unitCost":        unit_cost,
        "isAddOn":         is_add_on,
        "itemGroupName":   item_group_name,
        "itemGroupRootId": item_group_root_id,
        "employeeId":      technician_id,
        "order":           order,
    })
    if not body:
        return {"error": True, "detail": "Provide at least one field to update."}
    return _request(
        "PUT",
        _url("sales", f"estimates/{estimate_id}/items/{item_id}"),
        json=body,
    )


@mcp.tool()
def estimate_delete_item(estimate_id: int, item_id: int) -> Dict[str, Any]:
    """
    Remove a line item from an estimate.
    DELETE /sales/v2/tenant/{tenant}/estimates/{id}/items/{itemId}

    Args:
        estimate_id: ID of the estimate.
        item_id:     ID of the line item to delete.
    """
    return _request("DELETE", _url("sales", f"estimates/{estimate_id}/items/{item_id}"))


@mcp.tool()
def estimate_sell(
    estimate_id: int,
    sold_by_id: int,
    sold_on: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Mark an estimate as Sold. Requires a managed technician as the seller.
    When sold via API, work is flagged "perform later" and a CSR must book
    the resulting job.
    PUT /sales/v2/tenant/{tenant}/estimates/{id}/sell

    Args:
        estimate_id: ID of the estimate to sell.
        sold_by_id:  Managed technician ID who made the sale. Required by ST.
        sold_on:     ISO 8601 datetime of the sale (defaults to now if omitted).
    """
    body: Dict[str, Any] = {"soldById": sold_by_id}
    if sold_on is not None:
        body["soldOn"] = sold_on
    return _request("PUT", _url("sales", f"estimates/{estimate_id}/sell"), json=body)


@mcp.tool()
def estimate_unsell(estimate_id: int) -> Dict[str, Any]:
    """
    Revert a Sold estimate back to Open status.
    Only allowed when no digital signature has been collected and the estimate
    has not yet been booked into a separate job.
    PUT /sales/v2/tenant/{tenant}/estimates/{id}/unsell

    Args:
        estimate_id: ID of the estimate to unsell.
    """
    return _request("PUT", _url("sales", f"estimates/{estimate_id}/unsell"), json={})


@mcp.tool()
def estimate_dismiss(estimate_id: int, reason: Optional[str] = None) -> Dict[str, Any]:
    """
    Mark an estimate as Dismissed (no sale will be made).
    PUT /sales/v2/tenant/{tenant}/estimates/{id}/dismiss

    Args:
        estimate_id: ID of the estimate to dismiss.
        reason:      Optional text explaining why the estimate was dismissed.
    """
    body = _strip_none({"reason": reason})
    return _request("PUT", _url("sales", f"estimates/{estimate_id}/dismiss"), json=body)


# =============================================================================
#  INVOICES  (Accounting API v2)
#  Base path: /accounting/v2/tenant/{tenant}/invoices
# =============================================================================

@mcp.tool()
def invoice_update(
    invoice_id: int,
    summary: Optional[str] = None,
    payment_term_id: Optional[int] = None,
    purchase_order_number: Optional[str] = None,
    reviewed_by_id: Optional[int] = None,
    assigned_to_id: Optional[int] = None,
    invoice_date: Optional[str] = None,
    due_date: Optional[str] = None,
    royalty_status: Optional[str] = None,
    royalty_date: Optional[str] = None,
    royalty_memo: Optional[str] = None,
    batch_id: Optional[int] = None,
    import_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Update header-level fields on an existing invoice.
    PATCH /accounting/v2/tenant/{tenant}/invoices/{id}

    Args:
        invoice_id:            ID of the invoice.
        summary:               Free-text notes or summary on the invoice.
        payment_term_id:       Payment term ID (e.g. Net 30). Must exist in ST.
        purchase_order_number: Customer's external PO reference string.
        reviewed_by_id:        Employee ID who reviewed/approved the invoice.
        assigned_to_id:        Employee ID the invoice is assigned to for follow-up.
        invoice_date:          Invoice issue date (ISO 8601: YYYY-MM-DD).
        due_date:              Payment due date (ISO 8601: YYYY-MM-DD).
        royalty_status:        Royalty processing status string (e.g. "Pending").
        royalty_date:          Date used for royalty calculations (ISO 8601).
        royalty_memo:          Royalty-related memo text.
        batch_id:              Accounting batch this invoice belongs to.
        import_id:             External system reference / import identifier.
    """
    body = _strip_none({
        "summary":             summary,
        "paymentTermId":       payment_term_id,
        "purchaseOrderNumber": purchase_order_number,
        "reviewedById":        reviewed_by_id,
        "assignedToId":        assigned_to_id,
        "invoiceDate":         invoice_date,
        "dueDate":             due_date,
        "royaltyStatus":       royalty_status,
        "royaltyDate":         royalty_date,
        "royaltyMemo":         royalty_memo,
        "batchId":             batch_id,
        "importId":            import_id,
    })
    if not body:
        return {"error": True, "detail": "Provide at least one field to update."}
    return _request("PATCH", _url("accounting", f"invoices/{invoice_id}"), json=body)


@mcp.tool()
def invoice_update_custom_fields(
    invoice_id: int,
    custom_fields: List[Dict[str, str]],
) -> Dict[str, Any]:
    """
    Set or replace custom field values on an invoice.
    PATCH /accounting/v2/tenant/{tenant}/invoices/{id}

    Args:
        invoice_id:    ID of the invoice.
        custom_fields: List of dicts, one entry per field:
                       [{"name": "Project Code", "value": "PROJ-123"}, ...]
                       Pass value="" to clear a custom field.
    """
    if not custom_fields:
        return {"error": True, "detail": "custom_fields list cannot be empty."}
    return _request(
        "PATCH",
        _url("accounting", f"invoices/{invoice_id}"),
        json={"customFields": custom_fields},
    )


@mcp.tool()
def invoice_add_item(
    invoice_id: int,
    sku_id: int,
    quantity: float = 1.0,
    sku_name: Optional[str] = None,
    description: Optional[str] = None,
    unit_price: Optional[float] = None,
    unit_cost: Optional[float] = None,
    is_add_on: Optional[bool] = None,
    item_group_name: Optional[str] = None,
    item_group_root_id: Optional[int] = None,
    technician_id: Optional[int] = None,
    membership_type_id: Optional[int] = None,
    business_unit_id: Optional[int] = None,
    service_date: Optional[str] = None,
    order: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Append a line item to an invoice.
    PATCH /accounting/v2/tenant/{tenant}/invoices/{id}/items

    Args:
        invoice_id:          ID of the invoice.
        sku_id:              Pricebook SKU ID. Required.
        quantity:            Number of units (default 1).
        sku_name:            Display name override.
        description:         Line-item description.
        unit_price:          Price per unit (overrides pricebook).
        unit_cost:           Cost per unit for margin calculations.
        is_add_on:           Mark as an add-on item.
        item_group_name:     Group name for bundled items.
        item_group_root_id:  Root item ID of the group.
        technician_id:       Technician tied to this line item.
        membership_type_id:  Membership type for membership-related items.
        business_unit_id:    Business unit override for this line.
        service_date:        Date the service was performed (ISO 8601: YYYY-MM-DD).
        order:               Display order position.
    """
    # ST API: description and quantity are required; send fields at root level (no wrapper)
    body = _strip_none({
        "skuId":            sku_id,
        "quantity":         quantity,
        "skuName":          sku_name,
        "unitPrice":        unit_price,
        "cost":             unit_cost,
        "isAddOn":          is_add_on,
        "itemGroupName":    item_group_name,
        "itemGroupRootId":  item_group_root_id,
        "technicianId":     technician_id,
        "membershipTypeId": membership_type_id,
        "businessUnitId":   business_unit_id,
        "serviceDate":      service_date,
        "order":            order,
    })
    # description is always required by the API even if empty
    body["description"] = description or ""
    return _request("PATCH", _url("accounting", f"invoices/{invoice_id}/items"), json=body)


@mcp.tool()
def invoice_update_item(
    invoice_id: int,
    item_id: int,
    sku_id: Optional[int] = None,
    sku_name: Optional[str] = None,
    description: Optional[str] = None,
    unit_price: Optional[float] = None,
    quantity: Optional[float] = None,
    unit_cost: Optional[float] = None,
    is_add_on: Optional[bool] = None,
    item_group_name: Optional[str] = None,
    item_group_root_id: Optional[int] = None,
    technician_id: Optional[int] = None,
    membership_type_id: Optional[int] = None,
    business_unit_id: Optional[int] = None,
    service_date: Optional[str] = None,
    order: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Modify an existing line item on an invoice.
    PATCH /accounting/v2/tenant/{tenant}/invoices/{id}/items/{itemId}

    Args:
        invoice_id:          ID of the invoice.
        item_id:             ID of the line item to update.
        sku_id:              Replace with a different SKU.
        sku_name:            New display name.
        description:         New description.
        unit_price:          New price per unit.
        quantity:            New quantity.
        unit_cost:           New cost per unit.
        is_add_on:           Toggle add-on flag.
        item_group_name:     New item group name.
        item_group_root_id:  New item group root ID.
        technician_id:       Updated technician association.
        membership_type_id:  Updated membership type.
        business_unit_id:    Updated business unit.
        service_date:        Updated service date (ISO 8601: YYYY-MM-DD).
        order:               New display order.
    """
    body = _strip_none({
        "skuId":            sku_id,
        "skuName":          sku_name,
        "description":      description,
        "unitPrice":        unit_price,
        "quantity":         quantity,
        "unitCost":         unit_cost,
        "isAddOn":          is_add_on,
        "itemGroupName":    item_group_name,
        "itemGroupRootId":  item_group_root_id,
        "technicianId":     technician_id,
        "membershipTypeId": membership_type_id,
        "businessUnitId":   business_unit_id,
        "serviceDate":      service_date,
        "order":            order,
    })
    if not body:
        return {"error": True, "detail": "Provide at least one field to update."}
    return _request(
        "PATCH",
        _url("accounting", f"invoices/{invoice_id}/items/{item_id}"),
        json=body,
    )


@mcp.tool()
def invoice_items_list(invoice_id: int) -> Dict[str, Any]:
    """
    List all line items on an invoice.
    GET /accounting/v2/tenant/{tenant}/invoices/{id}/items

    Args:
        invoice_id: ID of the invoice.
    """
    return _request("GET", _url("accounting", f"invoices/{invoice_id}/items"))


@mcp.tool()
def invoice_delete_item(invoice_id: int, item_id: int) -> Dict[str, Any]:
    """
    Remove a line item from an invoice.
    DELETE /accounting/v2/tenant/{tenant}/invoices/{id}/items/{itemId}

    Args:
        invoice_id: ID of the invoice.
        item_id:    ID of the line item to remove.
    """
    return _request(
        "DELETE",
        _url("accounting", f"invoices/{invoice_id}/items/{item_id}"),
    )


@mcp.tool()
def invoice_post(invoice_id: int) -> Dict[str, Any]:
    """
    Post (finalize/lock) an invoice so it is ready for payment collection
    and general-ledger export. The existing connector's servicetitan_post_invoice
    covers this; this tool is provided for completeness within this connector.
    POST /accounting/v2/tenant/{tenant}/invoices/{id}/post

    Args:
        invoice_id: ID of the invoice to post.
    """
    return _request("POST", _url("accounting", f"invoices/{invoice_id}/post"), json={})


@mcp.tool()
def invoice_void(invoice_id: int, reason: Optional[str] = None) -> Dict[str, Any]:
    """
    Void an invoice. Used to write off uncollectable balances or reverse
    erroneous postings.
    POST /accounting/v2/tenant/{tenant}/invoices/{id}/void

    Args:
        invoice_id: ID of the invoice to void.
        reason:     Optional text reason for the void action.
    """
    body = _strip_none({"reason": reason})
    return _request("POST", _url("accounting", f"invoices/{invoice_id}/void"), json=body)


# =============================================================================
#  PURCHASE ORDERS  (Inventory API v2)
#  Base path: /inventory/v2/tenant/{tenant}/purchase-orders
#
#  CONFIRMED API NOTES (ServiceTitan V2 FAQ + developer docs):
#    - PO status cannot be changed via API; status is read-only.
#    - POs exported to an accounting system become fully read-only.
#    - Inventory receipts are a TOP-LEVEL resource at /inventory/v2/.../receipts,
#      not a sub-path under purchase-orders.
# =============================================================================

@mcp.tool()
def po_get_pdf(po_id: int, save_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Download a purchase order as a PDF from ServiceTitan.
    GET /inventory/v2/tenant/{tenant}/purchase-orders/{id}/pdf
    Returns the PDF saved to save_path (default: C:/ST/PO_{id}.pdf).
    """
    url = _url("inventory", f"purchase-orders/{po_id}/pdf")
    hdrs = {
        "Authorization": f"Bearer {_get_access_token()}",
        "ST-App-Key":    _require_env("ST_APP_KEY"),
        "Accept":        "application/pdf",
    }
    resp = httpx.get(url, headers=hdrs, timeout=30)
    if resp.status_code != 200:
        return {"error": True, "status_code": resp.status_code, "detail": resp.text[:500]}
    out = save_path or f"C:/ST/PO_{po_id}.pdf"
    with open(out, "wb") as f:
        f.write(resp.content)
    return {"saved": out, "size_bytes": len(resp.content)}


@mcp.tool()
def po_generate_pdf(po_id: int, save_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch a PO from ServiceTitan and generate a branded Denommee PDF.
    Pulls all real data (vendor, ship-to, technician, line items) from the ST API,
    then renders the PDF using generate_po_pdf.py.
    Saves to save_path or C:/Users/Ben/Downloads/PO_{id}.pdf.
    """
    import sys as _sys, os as _os

    # ── 1. Fetch PO header ────────────────────────────────────────────────────
    po = _request("GET", _url("inventory", f"purchase-orders/{po_id}"))
    if po.get("error"):
        return {"error": True, "step": "fetch_po", "detail": po}

    # ── 2. Fetch PO items ─────────────────────────────────────────────────────
    items_resp = _request("GET", _url("inventory", f"purchase-orders/{po_id}/items"),
                          params={"pageSize": 200})
    raw_items = items_resp.get("data", []) if not items_resp.get("error") else []

    # ── 3. Fetch vendor name ──────────────────────────────────────────────────
    vendor_id = po.get("vendorId")
    vendor_name = str(vendor_id)
    vendor_address = ""
    if vendor_id:
        vr = _request("GET", _url("inventory", f"vendors/{vendor_id}"))
        if not vr.get("error"):
            vendor_name = vr.get("name", vendor_name)
            a = vr.get("address") or {}
            vendor_address = ", ".join(filter(None, [
                a.get("street"), a.get("city"),
                f"{a.get('state','')} {a.get('zip','')}".strip(),
                a.get("country","USA"),
            ]))

    # ── 4. Build ship-to lines ────────────────────────────────────────────────
    ship = po.get("shipTo") or {}
    ship_addr = ship.get("address") or {}
    ship_line1 = ", ".join(filter(None, [
        ship_addr.get("street"), ship_addr.get("city"),
        f"{ship_addr.get('state','')} {ship_addr.get('zip','')}".strip(),
    ]))
    ship_line2 = ship_line1 + (f" {ship_addr.get('country','USA')}" if ship_line1 else "")

    # ── 5. Format dates ───────────────────────────────────────────────────────
    from datetime import datetime as _dt3
    def _fmt_date(s):
        if not s:
            return ""
        try:
            return _dt3.fromisoformat(s.replace("Z","")).strftime("%m/%d/%Y")
        except Exception:
            return s[:10]

    po_date       = _fmt_date(po.get("date",""))
    required_date = _fmt_date(po.get("requiredOn",""))

    # ── 6. Format items ───────────────────────────────────────────────────────
    job_id = po.get("jobId", "")
    formatted_items = []
    for it in raw_items:
        qty  = it.get("quantity", 1) or 1
        cost = it.get("cost", 0) or 0
        total = qty * cost
        formatted_items.append({
            "vendor_part":       it.get("vendorPartNumber", ""),
            "technician":        "",
            "st_part":           it.get("sku", {}).get("name", "") if isinstance(it.get("sku"), dict) else "",
            "item_name":         it.get("sku", {}).get("name", "") if isinstance(it.get("sku"), dict) else it.get("description",""),
            "item_description":  it.get("description", ""),
            "job_number":        job_id,
            "cost":              f"${cost:,.2f}",
            "quantity":          qty,
            "total":             f"${total:,.2f}",
        })

    # ── 7. Grand total ────────────────────────────────────────────────────────
    grand = sum((it.get("quantity",1) or 1) * (it.get("cost",0) or 0) for it in raw_items)

    # ── 8. Build po dict and generate PDF ────────────────────────────────────
    po_dict = {
        "po_number":      f"#{po_id}-001",
        "po_date":        po_date,
        "required_date":  required_date,
        "vendor_name":    vendor_name,
        "vendor_address": vendor_address,
        "ship_to_line1":  ship_line1,
        "ship_to_line2":  ship_line2,
        "technician":     po.get("memo", ""),
        "created_by":     "denommeeplumbingheating",
        "items":          formatted_items,
        "total":          f"${grand:,.2f}",
    }

    out = save_path or f"C:/Users/Ben/Downloads/PO_{po_id}.pdf"
    gen_path = _os.path.join(_os.path.dirname(__file__), "generate_po_pdf.py")
    _sys.path.insert(0, _os.path.dirname(__file__))
    from generate_po_pdf import generate_po_pdf as _gen
    _gen(po_dict, out)
    return {"saved": out, "po_number": po_dict["po_number"], "items": len(formatted_items), "total": po_dict["total"]}


@mcp.tool()
def po_create(
    type_id: int,
    vendor_id: int,
    business_unit_id: int,
    inventory_location_id: Optional[int] = None,
    job_id: Optional[int] = None,
    project_id: Optional[int] = None,
    technician_id: Optional[int] = None,
    required_on: Optional[str] = None,
    ship_to: Optional[str] = None,
    memo: Optional[str] = None,
    tax: Optional[float] = None,
    impacts_technician_payroll: Optional[bool] = None,
    shipping: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Create a new Purchase Order. New POs start in Pending status.
    POST /inventory/v2/tenant/{tenant}/purchase-orders

    Prerequisites: vendors, business units, and inventory locations must be
    configured in ServiceTitan before they can be referenced here.

    Use po_add_item() separately to add line items after creation.

    Args:
        type_id:                    PO type ID (configured in ST settings). Required.
        vendor_id:                  ID of the vendor supplying the items. Required.
        business_unit_id:           Business unit the PO costs are booked against. Required.
        inventory_location_id:      Inventory location ID for item delivery. Use inventory_locations_list() to find valid IDs.
        job_id:                     Associate PO with a job for cost reporting.
        project_id:                 Associate PO with a project.
        technician_id:              Associate PO with a technician.
        required_on:                Date items are needed (ISO 8601: YYYY-MM-DD).
        ship_to:                    Free-text shipping address override.
        memo:                       Internal notes visible on the PO.
        tax:                        Tax amount to apply to the PO total.
        impacts_technician_payroll: Whether PO costs affect tech pay (default False).
        shipping:                   Shipping cost to add to PO total.
    """
    from datetime import datetime as _dt
    today = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "typeId":                   type_id,
        "vendorId":                 vendor_id,
        "businessUnitId":           business_unit_id,
        "inventoryLocationId":      inventory_location_id,
        "jobId":                    job_id,
        "projectId":                project_id,
        "technicianId":             technician_id,
        "date":                     today,
        "requiredOn":               required_on or today,
        "shipTo":                   _parse_ship_to(ship_to),
        "memo":                     memo or "",
        "tax":                      tax if tax is not None else 0,
        "impactsTechnicianPayroll": impacts_technician_payroll if impacts_technician_payroll is not None else False,
        "shipping":                 shipping if shipping is not None else 0,
        "items":                    [],
    }
    # remove truly-None optional IDs
    for k in ("jobId", "projectId", "technicianId", "inventoryLocationId"):
        if body[k] is None:
            del body[k]
    return _request("POST", _url("inventory", "purchase-orders"), json=body)


@mcp.tool()
def po_update(
    po_id: int,
    type_id: Optional[int] = None,
    vendor_id: Optional[int] = None,
    business_unit_id: Optional[int] = None,
    location_id: Optional[int] = None,
    job_id: Optional[int] = None,
    project_id: Optional[int] = None,
    technician_id: Optional[int] = None,
    required_on: Optional[str] = None,
    ship_to: Optional[str] = None,
    memo: Optional[str] = None,
    tax: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Update header fields of an existing Purchase Order.
    PATCH /inventory/v2/tenant/{tenant}/purchase-orders/{id}

    Restrictions:
      - Status cannot be updated via API (read-only).
      - POs exported to an external accounting system are fully read-only.

    Args:
        po_id:            ID of the purchase order.
        type_id:          New PO type ID.
        vendor_id:        New vendor ID.
        business_unit_id: New business unit ID.
        location_id:      New ship-to inventory location ID.
        job_id:           Updated job association.
        project_id:       Updated project association.
        technician_id:    Updated technician association.
        required_on:      Updated required-by date (ISO 8601: YYYY-MM-DD).
        ship_to:          Updated free-text shipping address.
        memo:             Updated memo / notes.
        tax:              Updated tax amount.
    """
    body = _strip_none({
        "typeId":         type_id,
        "vendorId":       vendor_id,
        "businessUnitId": business_unit_id,
        "inventoryLocationId": location_id,
        "jobId":          job_id,
        "projectId":      project_id,
        "technicianId":   technician_id,
        "requiredOn":     required_on,
        "shipTo":         ship_to,
        "memo":           memo,
        "tax":            tax,
    })
    if not body:
        return {"error": True, "detail": "Provide at least one field to update."}
    return _request("PATCH", _url("inventory", f"purchase-orders/{po_id}"), json=body)


@mcp.tool()
def po_add_item(
    po_id: int,
    sku_id: int,
    quantity: float,
    unit_cost: Optional[float] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Add a line item to a Purchase Order.
    POST /inventory/v2/tenant/{tenant}/purchase-orders/{id}/items

    Args:
        po_id:        ID of the purchase order.
        sku_id:       Pricebook SKU ID of the item being ordered. Required.
        quantity:     Number of units to order. Required.
        unit_cost:    Cost per unit (overrides pricebook cost if provided).
        description:  Description override for this line item.
    """
    body = _strip_none({
        "skuId":       sku_id,
        "quantity":    quantity,
        "unitCost":    unit_cost,
        "description": description,
    })
    return _request("POST", _url("inventory", f"purchase-orders/{po_id}/items"), json=body)


@mcp.tool()
def po_update_item(
    po_id: int,
    item_id: int,
    sku_id: Optional[int] = None,
    quantity: Optional[float] = None,
    unit_cost: Optional[float] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Update an existing line item on a Purchase Order.
    PATCH /inventory/v2/tenant/{tenant}/purchase-orders/{id}/items/{itemId}

    Args:
        po_id:        ID of the purchase order.
        item_id:      ID of the line item to update.
        sku_id:       Replace with a different pricebook SKU.
        quantity:     New order quantity.
        unit_cost:    New per-unit cost.
        description:  Updated description.
    """
    body = _strip_none({
        "skuId":       sku_id,
        "quantity":    quantity,
        "unitCost":    unit_cost,
        "description": description,
    })
    if not body:
        return {"error": True, "detail": "Provide at least one field to update."}
    return _request(
        "PATCH",
        _url("inventory", f"purchase-orders/{po_id}/items/{item_id}"),
        json=body,
    )


@mcp.tool()
def po_delete_item(po_id: int, item_id: int) -> Dict[str, Any]:
    """
    Remove a line item from a Purchase Order.
    DELETE /inventory/v2/tenant/{tenant}/purchase-orders/{id}/items/{itemId}

    Args:
        po_id:    ID of the purchase order.
        item_id:  ID of the line item to remove.
    """
    return _request(
        "DELETE",
        _url("inventory", f"purchase-orders/{po_id}/items/{item_id}"),
    )


@mcp.tool()
def po_cancel(po_id: int) -> Dict[str, Any]:
    """
    Cancel a Purchase Order. Transitions its status to Canceled.
    The PO must not yet be received or exported.
    POST /inventory/v2/tenant/{tenant}/purchase-orders/{id}/cancel

    Args:
        po_id: ID of the purchase order to cancel.
    """
    return _request("POST", _url("inventory", f"purchase-orders/{po_id}/cancel"), json={})


@mcp.tool()
def po_create_receipt(
    purchase_order_id: int,
    items: List[Dict[str, Any]],
    location_id: Optional[int] = None,
    received_on: Optional[str] = None,
    memo: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Record receipt of items from a Purchase Order. This creates a Receipt
    (a top-level inventory resource) and advances the PO toward
    Received / Partially Received status.

    POST /inventory/v2/tenant/{tenant}/receipts

    Note: Receipts are a top-level resource in the ServiceTitan Inventory API,
    not a sub-path under purchase-orders.

    Args:
        purchase_order_id: ID of the source Purchase Order.
        items:             List of received items:
                           [{"purchaseOrderItemId": int, "quantity": float}, ...]
                           Use the line item ID from po_update_item (not the SKU ID).
        location_id:       Inventory location where items are received into stock.
        received_on:       ISO 8601 datetime of physical receipt (defaults to now).
        memo:              Notes about the receipt (e.g., partial shipment reason).
    """
    if not items:
        return {"error": True, "detail": "items list cannot be empty."}
    body: Dict[str, Any] = {
        "purchaseOrderId": purchase_order_id,
        "items":           items,
    }
    body.update(_strip_none({
        "locationId": location_id,
        "receivedOn": received_on,
        "memo":       memo,
    }))
    return _request("POST", _url("inventory", "receipts"), json=body)


# =============================================================================
#  TIMESHEETS  (Payroll API v2)
#  Base path: /payroll/v2/tenant/{tenant}/timesheets
#
#  IMPORTANT: If the tenant has "Flexible Timekeeping" enabled in ST settings,
#  create/update endpoints return HTTP 400 with "Timesheet action cannot be
#  approved because flexible timekeeping is enabled."  Verify tenant config
#  before using timesheet_create and timesheet_update.
# =============================================================================

@mcp.tool()
def timesheet_list(
    technician_id: Optional[int] = None,
    started_on_or_after: Optional[str] = None,
    started_on_or_before: Optional[str] = None,
    timesheet_code_id: Optional[int] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    List NON-JOB timesheet entries (PTO, shop time, meetings, training).
    GET /payroll/v2/tenant/{tenant}/non-job-timesheets

    NOTE: For a complete timecard you must ALSO call gross_pay_items_list for job hours.
    Date filters do NOT work server-side -- filter client-side by startedOn field.
    Duration = endedOn minus startedOn for each record.

    Args:
        technician_id:         Filter by technician/employee ID.
        started_on_or_after:   Lower bound (ignored by API, filter client-side).
        started_on_or_before:  Upper bound (ignored by API, filter client-side).
        timesheet_code_id:     Filter to a specific event type (timesheet code).
        page:                  1-based page number.
        page_size:             Results per page (max 1000).
    """
    params = _strip_none({
        "employeeId":         technician_id,
        "employeeType":       "Technician",
        "startedOnOrAfter":   started_on_or_after,
        "startedOnOrBefore":  started_on_or_before,
        "timesheetCodeId":    timesheet_code_id,
        "page":               page,
        "pageSize":           page_size,
    })
    return _request("GET", _url("payroll", "non-job-timesheets"), params=params)


@mcp.tool()
def timesheet_get(timesheet_id: int) -> Dict[str, Any]:
    """
    Fetch a single timesheet entry by ID.
    GET /payroll/v2/tenant/{tenant}/timesheets/{id}

    Args:
        timesheet_id: ID of the timesheet entry.
    """
    return _request("GET", _url("payroll", f"non-job-timesheets/{timesheet_id}"))


@mcp.tool()
def timesheet_create(
    technician_id: int,
    timesheet_code_id: int,
    started_on: str,
    ended_on: Optional[str] = None,
    note: Optional[str] = None,
    is_payable: Optional[bool] = None,
    is_holiday: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Create a non-job timesheet entry (e.g., meeting, shop time, training).
    POST /payroll/v2/tenant/{tenant}/timesheets

    WARNING: Returns HTTP 400 if the tenant has Flexible Timekeeping enabled.
    Check ServiceTitan Settings > Payroll before using in production.

    Args:
        technician_id:     ID of the technician this time belongs to. Required.
        timesheet_code_id: ID of the timesheet code (event type). Required.
                           Examples: "Meeting", "Shop Time", "Holiday".
                           Retrieve available codes from the read connector.
        started_on:        Punch-in datetime. ISO 8601 with timezone recommended,
                           e.g. "2026-05-27T08:00:00-07:00". Required.
        ended_on:          Punch-out datetime (ISO 8601). Omit to leave open
                           (technician is still clocked in).
        note:              Free-text notes about this time block.
        is_payable:        Override whether this time is paid. Uses the
                           timesheet code's default setting if omitted.
        is_holiday:        Flag this entry as holiday time.
    """
    body = _strip_none({
        "employeeId":      technician_id,
        "timesheetCodeId": timesheet_code_id,
        "startedOn":       started_on,
        "endedOn":         ended_on,
        "note":            note,
        "isPayable":       is_payable,
        "isHoliday":       is_holiday,
    })
    return _request("POST", _url("payroll", "non-job-timesheets"), json=body)


@mcp.tool()
def timesheet_update(
    timesheet_id: int,
    timesheet_code_id: Optional[int] = None,
    started_on: Optional[str] = None,
    ended_on: Optional[str] = None,
    note: Optional[str] = None,
    is_payable: Optional[bool] = None,
    is_holiday: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Update an existing non-job timesheet entry.
    PATCH /payroll/v2/tenant/{tenant}/timesheets/{id}

    WARNING: Returns HTTP 400 if the tenant has Flexible Timekeeping enabled.

    Args:
        timesheet_id:      ID of the entry to update.
        timesheet_code_id: Change the event type code.
        started_on:        New punch-in datetime (ISO 8601).
        ended_on:          New punch-out datetime (ISO 8601).
        note:              Updated notes text.
        is_payable:        Update the payable flag.
        is_holiday:        Update the holiday flag.
    """
    body = _strip_none({
        "timesheetCodeId": timesheet_code_id,
        "startedOn":       started_on,
        "endedOn":         ended_on,
        "note":            note,
        "isPayable":       is_payable,
        "isHoliday":       is_holiday,
    })
    if not body:
        return {"error": True, "detail": "Provide at least one field to update."}
    return _request("PATCH", _url("payroll", f"non-job-timesheets/{timesheet_id}"), json=body)


@mcp.tool()
def timesheet_delete(timesheet_id: int) -> Dict[str, Any]:
    """
    Delete a timesheet entry.
    DELETE /payroll/v2/tenant/{tenant}/timesheets/{id}

    Args:
        timesheet_id: ID of the timesheet entry to delete.
    """
    return _request("DELETE", _url("payroll", f"non-job-timesheets/{timesheet_id}"))


@mcp.tool()
def timesheet_approve(timesheet_id: int) -> Dict[str, Any]:
    """
    Approve a single timesheet entry, locking it for payroll processing.
    PATCH /payroll/v2/tenant/{tenant}/timesheets/{id}
    (Sends isApproved: true -- ServiceTitan approves entries via PATCH.)

    Args:
        timesheet_id: ID of the timesheet entry to approve.
    """
    return _request(
        "PATCH",
        _url("payroll", f"non-job-timesheets/{timesheet_id}"),
        json={"isApproved": True},
    )


@mcp.tool()
def timesheet_approve_many(timesheet_ids: List[int]) -> Dict[str, Any]:
    """
    Approve multiple timesheet entries. Calls the individual approve endpoint
    for each ID and returns a summary of successes and failures.

    Args:
        timesheet_ids: List of timesheet entry IDs to approve.
                       Example: [1001, 1002, 1003]
    """
    if not timesheet_ids:
        return {"error": True, "detail": "timesheet_ids list cannot be empty."}

    results: List[Dict[str, Any]] = []
    for tid in timesheet_ids:
        result = _request(
            "PATCH",
            _url("payroll", f"non-job-timesheets/{tid}"),
            json={"isApproved": True},
        )
        results.append({"timesheetId": tid, "result": result})

    successes = [r for r in results if not r["result"].get("error")]
    failures  = [r for r in results if r["result"].get("error")]

    return {
        "total":      len(timesheet_ids),
        "succeeded":  len(successes),
        "failed":     len(failures),
        "results":    results,
    }


@mcp.tool()
def timesheet_update_job_entry(
    job_id: int,
    timesheet_id: int,
    started_on: Optional[str] = None,
    ended_on: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Correct or adjust a technician's time-on-job punch record.
    These are the punch-in/out records generated when a tech works on a job,
    distinct from non-job activity timesheets.
    PATCH /jpm/v2/tenant/{tenant}/jobs/{jobId}/timesheets/{timesheetId}

    Args:
        job_id:        ID of the job.
        timesheet_id:  ID of the timesheet record within the job.
        started_on:    Corrected punch-in time (ISO 8601 datetime).
        ended_on:      Corrected punch-out time (ISO 8601 datetime).
        note:          Notes for the correction (e.g. "Corrected by manager").
    """
    body = _strip_none({
        "startedOn": started_on,
        "endedOn":   ended_on,
        "note":      note,
    })
    if not body:
        return {"error": True, "detail": "Provide at least one field to update."}
    return _request(
        "PATCH",
        _url("jpm", f"jobs/{job_id}/timesheets/{timesheet_id}"),
        json=body,
    )


@mcp.tool()
def payroll_adjustment_create(
    technician_id: int,
    activity_code_id: int,
    amount: float,
    note: Optional[str] = None,
    date: Optional[str] = None,
    hours: Optional[float] = None,
    rate: Optional[float] = None,
    payroll_period_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Create a manual payroll adjustment (bonus, commission top-up, correction,
    spiff, deduction, etc.) for a technician.
    POST /payroll/v2/tenant/{tenant}/payroll-adjustments

    Confirmed supported in ServiceTitan V2 per "Add or delete payroll
    adjustments" in the official Payroll API documentation.

    Args:
        technician_id:     ID of the technician. Required.
        activity_code_id:  Earning/activity code ID that categorizes this
                           adjustment. Must be configured in ST payroll settings.
                           Required.
        amount:            Dollar amount. Use a negative value for deductions.
                           Required.
        note:              Description or reason for the adjustment.
        date:              Effective date (ISO 8601: YYYY-MM-DD).
        hours:             Number of hours (for hourly-rate adjustments).
        rate:              Hourly rate (when combined with hours, amount =
                           hours x rate).
        payroll_period_id: Payroll period to post this adjustment into.
                           If omitted, ST posts to the current open period.
    """
    body = _strip_none({
        "employeeId":      technician_id,
        "activityCodeId":  activity_code_id,
        "amount":          amount,
        "note":            note,
        "date":            date,
        "hours":           hours,
        "rate":            rate,
        "payrollPeriodId": payroll_period_id,
    })
    return _request("POST", _url("payroll", "payroll-adjustments"), json=body)


@mcp.tool()
def payroll_adjustment_delete(adjustment_id: int) -> Dict[str, Any]:
    """
    Remove a manually created payroll adjustment.
    Only adjustments that have not yet been processed in a closed payroll
    period can be deleted.
    DELETE /payroll/v2/tenant/{tenant}/payroll-adjustments/{id}

    Args:
        adjustment_id: ID of the payroll adjustment to delete.
    """
    return _request("DELETE", _url("payroll", f"payroll-adjustments/{adjustment_id}"))




@mcp.tool()
def cache_lookup(entity_type: str, name: str) -> Dict[str, Any]:
    """
    Look up any entity by name from the local SQLite cache.
    ALWAYS use this before calling any tool that needs an ID -- never ask the user for an ID.
    Falls back to error message if cache unavailable.

    entity_type options:
        "technicians"          - look up technician name -> id
        "customers"            - look up customer name -> id
        "locations"            - look up by name, address, or city -> id + customer_id
        "vendors"              - look up vendor name -> id
        "business_units"       - look up business unit name -> id
        "po_types"             - look up PO type name -> id
        "pricebook_services"   - look up labor/service item by name or code -> id (skuId)
        "pricebook_materials"  - look up material/part by name or code -> id (skuId)
        "pricebook_equipment"  - look up equipment item by name or code -> id (skuId)
        "job_types"            - look up job type name -> id
        "campaigns"            - look up marketing campaign name -> id
        "tag_types"            - look up job/customer tag type name -> id
        "job_cancel_reasons"   - look up cancel reason name -> id
        "membership_types"     - look up membership type name -> id

    Args:
        entity_type: One of the types listed above.
        name:        Full or partial name to search (case-insensitive).
    """
    import sqlite3
    db = r"C:/ST/st_cache.db"
    valid = ["technicians","customers","locations","vendors","business_units",
             "po_types","inventory_locations","pricebook_services","pricebook_materials",
             "pricebook_equipment","job_types","campaigns","tag_types",
             "job_cancel_reasons","membership_types"]
    if entity_type not in valid:
        return {"error": True, "detail": f"entity_type must be one of: {valid}"}
    try:
        conn = sqlite3.connect(db)
        if entity_type == "locations":
            rows = conn.execute(
                "SELECT * FROM locations WHERE name LIKE ? OR address LIKE ? OR city LIKE ? COLLATE NOCASE LIMIT 20",
                (f"%{name}%", f"%{name}%", f"%{name}%")
            ).fetchall()
        elif entity_type in ("pricebook_services","pricebook_materials","pricebook_equipment"):
            rows = conn.execute(
                f"SELECT * FROM {entity_type} WHERE (name LIKE ? OR code LIKE ?) COLLATE NOCASE LIMIT 20",
                (f"%{name}%", f"%{name}%")
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM {entity_type} WHERE name LIKE ? COLLATE NOCASE LIMIT 20",
                (f"%{name}%",)
            ).fetchall()
        cols = [d[0] for d in conn.execute(f"PRAGMA table_info({entity_type})").fetchall()]
        conn.close()
        return {"entity_type": entity_type, "results": [dict(zip(cols, r)) for r in rows]}
    except Exception as e:
        return {"error": True, "detail": str(e)}


@mcp.tool()
def job_lookup(
    customer_name: Optional[str] = None,
    job_number: Optional[str] = None,
    status: Optional[str] = None,
    page_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Search for jobs by customer name, job number, or status.
    Use this to find a jobId and invoiceId before adding labor or items to an invoice.
    Returns jobId, jobNumber, customerName, locationAddress, status, invoiceId.

    Args:
        customer_name: Partial customer name to search.
        job_number:    Exact or partial job number.
        status:        e.g. "InProgress", "Completed", "Scheduled".
        page_size:     Results per page (default 25).
    """
    params = _strip_none({
        "name":      customer_name,
        "number":    job_number,
        "jobStatus": status,
        "pageSize":  page_size or 25,
    })
    return _request("GET", _url("jpm", "jobs"), params=params)

@mcp.tool()
def gross_pay_items_list(
    technician_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    List job-based hours and pay for a technician (gross pay items).
    GET /payroll/v2/tenant/{tenant}/gross-pay-items

    NOTE: For a complete timecard you must ALSO call timesheet_list for non-job time.
    date_from uses modifiedOnOrAfter filter -- set it ~10 days before period start,
    then filter client-side by the date field for the exact range you want.
    paidDurationHours = hours worked. jobNumber + customerName = job info.

    Args:
        technician_id: Filter by technician/employee ID.
        date_from:     Fetch records modified on or after this date (YYYY-MM-DD).
        page:          1-based page number.
        page_size:     Results per page (max 1000).
    """
    params = _strip_none({
        "employeeId":      technician_id,
        "dateOnOrAfter":   date_from,
        "dateOnOrBefore":  date_to,
        "page":            page,
        "pageSize":        page_size,
    })
    return _request("GET", _url("payroll", "gross-pay-items"), params=params)

@mcp.tool()
def payroll_adjustment_list(
    technician_id: Optional[int] = None,
    payroll_period_id: Optional[int] = None,
    date_on_or_after: Optional[str] = None,
    date_on_or_before: Optional[str] = None,
    page: Optional[int] = None,
    page_size: Optional[int] = None,
) -> Dict[str, Any]:
    """
    List payroll adjustments with optional filters.
    GET /payroll/v2/tenant/{tenant}/payroll-adjustments

    Args:
        technician_id:      Filter by technician ID.
        payroll_period_id:  Filter by payroll period ID.
        date_on_or_after:   ISO 8601 date lower bound on adjustment effective date.
        date_on_or_before:  ISO 8601 date upper bound on adjustment effective date.
        page:               1-based page number.
        page_size:          Results per page (max 1000).
    """
    params = _strip_none({
        "employeeId":      technician_id,
        "payrollPeriodId": payroll_period_id,
        "dateOnOrAfter":   date_on_or_after,
        "dateOnOrBefore":  date_on_or_before,
        "page":            page,
        "pageSize":        page_size,
    })
    return _request("GET", _url("payroll", "payroll-adjustments"), params=params)


# (entry point moved to end)


# -----------------------------------------------------------------------------
#  Lookup helpers (vendors, PO types, inventory locations)
# -----------------------------------------------------------------------------

@mcp.tool()
def vendor_list(page: int = 1, page_size: int = 200) -> Dict[str, Any]:
    """List vendors (suppliers) - returns id, name, and contact info."""
    return _request("GET", _url("inventory", "vendors"), params={"page": page, "pageSize": page_size})


@mcp.tool()
def vendor_search(name: str) -> Dict[str, Any]:
    """
    Find a vendor by name — checks local cache first (instant), falls back to live API.
    Use this to get a vendor ID before calling po_create or po_create_smart.

    Args:
        name: Partial vendor name, e.g. "FW Webb", "Enterprise Bank"
    """
    hits = _cache_resolve("vendors", name)
    if hits:
        return {"source": "cache", "results": hits}
    # Cache miss — hit the API
    vr = _request("GET", _url("inventory", "vendors"),
                  params={"page": 1, "pageSize": 500})
    if "error" in vr:
        return vr
    import re as _re
    def _norm(s): return _re.sub(r'[^a-z0-9 ]', '', s.lower())
    matches = [v for v in vr.get("data", []) if _norm(name) in _norm(v.get("name", ""))]
    return {"source": "api", "results": matches}


@mcp.tool()
def resolve_name(entity_type: str, name: str) -> Dict[str, Any]:
    """
    General-purpose name → ID resolver.  Checks local cache first, API fallback.
    Useful for finding any entity ID without burning an API call.

    entity_type options:
        vendors, technicians, business_units, job_types, customers,
        locations, po_types, pricebook_services, pricebook_materials,
        pricebook_equipment, inventory_locations, campaigns,
        tag_types, job_cancel_reasons, membership_types

    Args:
        entity_type: One of the entity types listed above.
        name:        Partial name to search for (case-insensitive).

    Returns dict with "source" ("cache" or "api_not_supported"), "results" list,
    and "best_id" (ID of the best match, or null).
    """
    hits = _cache_resolve(entity_type, name)
    best_id = _cache_resolve_id(entity_type, name) if hits else None
    return {
        "source": "cache" if hits else "not_found",
        "entity_type": entity_type,
        "query": name,
        "best_id": best_id,
        "results": hits,
    }


@mcp.tool()
def po_types_list(page: int = 1, page_size: int = 200) -> Dict[str, Any]:
    """List purchase order types - returns id and name (e.g. Supply House Run, Special Order)."""
    return _request("GET", _url("inventory", "purchase-order-types"), params={"page": page, "pageSize": page_size})


@mcp.tool()
def inventory_locations_list(page: int = 1, page_size: int = 200) -> Dict[str, Any]:
    """List inventory locations (warehouses) - returns id and name for use in po_create inventoryLocationId field."""
    return _request("GET", _url("inventory", "warehouses"), params={"page": page, "pageSize": page_size})



# -----------------------------------------------------------------------------
#  Smart PO creation (single-step)
# -----------------------------------------------------------------------------

def _parse_ship_to(ship_to):
    """Convert ship_to string or dict to ST CreateAddressRequest format: {description, address: {street, ...}}"""
    if not ship_to:
        addr = {"street": "", "unit": "", "city": "", "state": "", "zip": "", "country": "USA"}
        return {"description": "", "address": addr}
    if isinstance(ship_to, dict) and "address" in ship_to:
        return ship_to  # already in correct format
    if isinstance(ship_to, dict):
        # flat address dict — wrap it
        return {"description": ship_to.get("street", ""), "address": ship_to}
    # Parse string: "123 Main St, City, ST 01234"
    import re as _re2
    parts = [p.strip() for p in ship_to.split(",")]
    street = parts[0] if len(parts) > 0 else ship_to
    city = parts[1] if len(parts) > 1 else ""
    state_zip = parts[2].strip() if len(parts) > 2 else ""
    m = _re2.match(r'([A-Za-z]{2})\s+(\S+)', state_zip)
    state = m.group(1).upper() if m else state_zip
    zip_code = m.group(2) if m else ""
    addr = {"street": street, "unit": "", "city": city, "state": state, "zip": zip_code, "country": "USA"}
    return {"description": ship_to, "address": addr}


@mcp.tool()
def po_create_smart(
    vendor_name: str,
    po_type_name: str,
    job_id: int,
    memo: Optional[str] = None,
    required_on: Optional[str] = None,
    ship_to: Optional[str] = None,
    tax: Optional[float] = None,
    inventory_location_id: Optional[int] = None,
    impacts_technician_payroll: Optional[bool] = None,
    shipping: Optional[float] = None,
    items: Optional[list] = None,
) -> Dict[str, Any]:
    """
    Create a PO in one step using plain names instead of IDs.
    Checks local cache (C:/ST/st_cache.db) first for speed,
    falls back to live API if cache is unavailable or item not found.

    Args:
        vendor_name:                Vendor name (partial match OK, e.g. "FW Webb").
        po_type_name:               PO type name (e.g. "Supply House Run", "Special Order").
        items:                      List of item dicts: [{"skuId": 123, "quantity": 1, "description": "...", "cost": 0, "vendorPartNumber": ""}]
        job_id:                     Job ID to link the PO to.
        memo:                       Optional internal notes.
        required_on:                Optional date items needed (YYYY-MM-DD).
        ship_to:                    Optional shipping address override.
        tax:                        Optional tax amount.
        inventory_location_id:      Optional inventory location ID (use inventory_locations_list to find).
        impacts_technician_payroll: Optional flag for payroll impact.
        shipping:                   Optional shipping cost.
    """
    vendor_id = None
    vendor_found = None
    type_id = None
    type_found = None

    # --- Step 1: find vendor (cache first, API fallback) ---
    cache_hits = _cache_resolve("vendors", vendor_name)
    if cache_hits:
        vendor_id, vendor_found = cache_hits[0]["id"], cache_hits[0]["name"]
    else:
        vr = _request("GET", _url("inventory", "vendors"), params={"page": 1, "pageSize": 500})
        if "error" in vr:
            return {"error": True, "step": "vendor_lookup", "detail": vr}
        import re as _re
        def _norm(s): return _re.sub(r'[^a-z0-9 ]', '', s.lower())
        matches = [v for v in vr.get("data", []) if _norm(vendor_name) in _norm(v.get("name", ""))]
        if not matches:
            return {"error": True, "detail": f"No vendor found matching '{vendor_name}'"}
        vendor_id, vendor_found = matches[0]["id"], matches[0]["name"]

    # --- Step 2: find PO type (cache first, API fallback) ---
    type_hits = _cache_resolve("po_types", po_type_name)
    if type_hits:
        type_id, type_found = type_hits[0]["id"], type_hits[0]["name"]
    else:
        tr = _request("GET", _url("inventory", "purchase-order-types"), params={"page": 1, "pageSize": 200})
        if "error" in tr:
            return {"error": True, "step": "po_type_lookup", "detail": tr}
        tmatches = [t for t in tr.get("data", []) if po_type_name.lower() in t.get("name", "").lower()]
        if not tmatches:
            return {"error": True, "detail": f"No PO type found matching '{po_type_name}'"}
        type_id, type_found = tmatches[0]["id"], tmatches[0]["name"]

    # --- Step 3: get business unit from job (always live - job data changes) ---
    jr = _request("GET", _url("jpm", f"jobs/{job_id}"))
    if "error" in jr:
        return {"error": True, "step": "job_lookup", "detail": jr}
    business_unit_id = jr.get("businessUnitId")
    if not business_unit_id:
        return {"error": True, "detail": "Could not find businessUnitId on job"}

    # --- Step 4: create the PO ---
    from datetime import datetime as _dt2
    _today = _dt2.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "typeId":                   type_id,
        "vendorId":                 vendor_id,
        "businessUnitId":           business_unit_id,
        "jobId":                    job_id,
        "date":                     _today,
        "requiredOn":               required_on or _today,
        "shipTo":                   _parse_ship_to(ship_to),
        "memo":                     memo or "",
        "tax":                      tax if tax is not None else 0,
        "inventoryLocationId":      inventory_location_id,
        "impactsTechnicianPayroll": impacts_technician_payroll if impacts_technician_payroll is not None else False,
        "shipping":                 shipping if shipping is not None else 0,
        "items":                    [
            {
                "skuId": i.get("skuId"),
                "quantity": i.get("quantity", 1),
                "description": i.get("description", ""),
                "cost": i.get("cost", i.get("unitCost", 0)),
                "vendorPartNumber": i.get("vendorPartNumber", ""),
            }
            for i in (items or [])
        ],
    }
    if body["inventoryLocationId"] is None:
        del body["inventoryLocationId"]
    result = _request("POST", _url("inventory", "purchase-orders"), json=body)
    result["_resolved"] = {
        "vendor":            vendor_found,
        "vendor_id":         vendor_id,
        "po_type":           type_found,
        "po_type_id":        type_id,
        "business_unit_id":  business_unit_id,
    }
    return result

# =============================================================================
#  JOBS  (JPM API v2)
#  Base path: /jpm/v2/tenant/{tenant}/jobs
# =============================================================================

@mcp.tool()
def job_types_list() -> Dict[str, Any]:
    """
    List all job types configured in ServiceTitan.
    GET /jpm/v2/tenant/{tenant}/job-types
    Returns id, name, businessUnitIds for each type.
    """
    result = _request("GET", _url("jpm", "job-types"), params={"pageSize": 200})
    return result


@mcp.tool()
def job_create(
    customer_id: int,
    location_id: int,
    business_unit_id: int,
    job_type_id: int,
    campaign_id: int,
    summary: Optional[str] = None,
    priority: Optional[str] = "Normal",
    customer_po: Optional[str] = None,
    appointment_start: Optional[str] = None,
    appointment_end: Optional[str] = None,
    technician_ids: Optional[list] = None,
) -> Dict[str, Any]:
    """
    Create a new job in ServiceTitan.
    POST /jpm/v2/tenant/{tenant}/jobs

    The ST API requires at least one appointment. If appointment_start/end are
    not provided, a placeholder appointment is created for today (unscheduled).
    appointment_start / appointment_end: ISO 8601 strings, e.g. "2026-05-30T09:00:00Z"

    Returns the full response body on success, or an error dict with the
    full API error detail so you can diagnose what field is missing.
    """
    from datetime import datetime, timezone
    if not appointment_start:
        # Default: unscheduled placeholder — start = now, end = now+1h
        now = datetime.now(timezone.utc)
        appointment_start = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        appointment_end   = now.replace(hour=now.hour + 1 if now.hour < 23 else 23,
                                        minute=0, second=0).strftime("%Y-%m-%dT%H:%M:%SZ") \
                            if not appointment_end else appointment_end

    body = _strip_none({
        "customerId":     customer_id,
        "locationId":     location_id,
        "businessUnitId": business_unit_id,
        "jobTypeId":      job_type_id,
        "campaignId":     campaign_id,
        "summary":        summary,
        "priority":       priority,
        "customerPo":     customer_po,
        "appointments": [
            _strip_none({
                "start": appointment_start,
                "end":   appointment_end,
                "arrivalWindowStart": appointment_start,
                "arrivalWindowEnd":   appointment_end,
                "technicianIds": technician_ids,
            })
        ],
    })
    return _request("POST", _url("jpm", "jobs"), json=body)


@mcp.tool()
def appointment_set_status(appointment_id: int, status: str) -> Dict[str, Any]:
    """
    NOTE: This endpoint does NOT exist in the ServiceTitan Dispatch API.
    Appointment status (Scheduled, Dispatched, Working, Done, etc.) is a derived,
    read-only value driven by technician assignment activity — it cannot be set directly
    via API. Assigning a technician triggers 'Dispatched'; techs completing via mobile
    triggers 'Done'. There is no POST /appointments/{id}/status-changes endpoint.

    This function is intentionally disabled and returns an informative error.
    Use appointment_assign_technician or appointment_reschedule for dispatch actions.

    Args:
        appointment_id: The appointment ID.
        status:         Would-be status string (ignored).
    """
    return {
        "error": "NOT_SUPPORTED",
        "message": (
            "appointment_set_status cannot be implemented — the ServiceTitan Dispatch API "
            "has no endpoint to directly set appointment status. Status is derived from "
            "technician assignment activity. Use appointment_assign_technician to dispatch "
            "a tech (sets status to Dispatched), or appointment_reschedule to change timing."
        ),
    }


@mcp.tool()
def appointment_assign_technician(
    appointment_id: int,
    technician_ids: list,
) -> Dict[str, Any]:
    """
    Assign one or more technicians to an existing appointment.
    Uses the Dispatch API appointment-assignments endpoint.
    POST /dispatch/v2/tenant/{tenant}/appointment-assignments
    technician_ids: list of technician employee IDs, e.g. [38472702]
    """
    # ST Dispatch API: POST /dispatch/v2/tenant/{tenant}/appointment-assignments/assign-technicians
    body = {"jobAppointmentId": appointment_id, "technicianIds": technician_ids}
    return _request("POST", _url("dispatch", "appointment-assignments/assign-technicians"), json=body)


@mcp.tool()
def appointment_assignments_list(appointment_id: int) -> Dict[str, Any]:
    """
    Read current technician assignments for an appointment.
    GET /dispatch/v2/tenant/{tenant}/appointment-assignments?appointmentIds={id}
    Use this to verify whether assign_technician actually persisted.
    """
    return _request("GET", _url("dispatch", "appointment-assignments"),
                    params={"appointmentIds": appointment_id, "pageSize": 50})


@mcp.tool()
def appointment_add(
    job_id: int,
    start: str,
    end: str,
    technician_ids: Optional[list] = None,
    special_instructions: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Add a new appointment to an existing job via the JPM API.
    POST /jpm/v2/tenant/{tenant}/appointments
    start/end: ISO 8601 UTC strings, e.g. "2026-06-02T14:00:00Z"
    technician_ids: list of tech IDs to assign at creation time.
    """
    body = _strip_none({
        "jobId": job_id,
        "start": start,
        "end": end,
        "arrivalWindowStart": start,
        "arrivalWindowEnd": end,
        "technicianIds": technician_ids,
        "specialInstructions": special_instructions,
    })
    return _request("POST", _url("jpm", "appointments"), json=body)


# =============================================================================
#  DISPATCH API — additional endpoints
# =============================================================================

@mcp.tool()
def appointment_unassign_technician(appointment_id: int, technician_ids: list) -> Dict[str, Any]:
    """Unassign technicians from an appointment. POST /dispatch/v2/.../appointment-assignments/unassign-technicians"""
    return _request("POST", _url("dispatch", "appointment-assignments/unassign-technicians"),
                    json={"jobAppointmentId": appointment_id, "technicianIds": technician_ids})

@mcp.tool()
def dispatch_non_job_appointments_list(page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    """List non-job appointments. GET /dispatch/v2/.../non-job-appointments"""
    return _request("GET", _url("dispatch", "non-job-appointments"), params={"page": page, "pageSize": page_size})

@mcp.tool()
def dispatch_non_job_appointment_create(
    technician_id: int,
    start: str,
    end: str,
    name: str = "Time Block",
    summary: Optional[str] = None,
    activity_code_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Create a non-job appointment (time block). POST /dispatch/v2/.../non-job-appointments
    start/end: ISO 8601, e.g. '2026-06-01T08:00:00Z'. name is required by ST API (defaults to 'Time Block').
    The API takes duration as a TimeSpan string (HH:MM:SS) computed from start/end."""
    from datetime import datetime
    def _parse_iso(s: str):
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    delta = _parse_iso(end) - _parse_iso(start)
    total_secs = int(delta.total_seconds())
    hours, remainder = divmod(total_secs, 3600)
    minutes, seconds = divmod(remainder, 60)
    duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    body = _strip_none({"technicianId": technician_id, "start": start,
                         "duration": duration_str, "name": name,
                         "summary": summary, "timesheetCodeId": activity_code_id})
    return _request("POST", _url("dispatch", "non-job-appointments"), json=body)

@mcp.tool()
def dispatch_non_job_appointment_update(appointment_id: int,
    technician_id: Optional[int] = None,
    start: Optional[str] = None, end: Optional[str] = None,
    name: Optional[str] = None, summary: Optional[str] = None) -> Dict[str, Any]:
    """Update a non-job appointment. PUT /dispatch/v2/.../non-job-appointments/{id}
    NOTE: ST PUT requires technicianId, start, duration, name. Fetch the existing record first
    to populate any field you are not changing. Pass end to recompute duration."""
    body: Dict[str, Any] = {}
    if technician_id is not None:
        body["technicianId"] = technician_id
    if start is not None:
        body["start"] = start
    if end is not None and start is not None:
        from datetime import datetime
        def _parse_iso(s: str):
            s = s.replace("Z", "+00:00")
            return datetime.fromisoformat(s)
        delta = _parse_iso(end) - _parse_iso(start)
        total_secs = int(delta.total_seconds())
        hours, remainder = divmod(total_secs, 3600)
        minutes, seconds = divmod(remainder, 60)
        body["duration"] = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    if name is not None:
        body["name"] = name
    if summary is not None:
        body["summary"] = summary
    return _request("PUT", _url("dispatch", f"non-job-appointments/{appointment_id}"), json=body)

@mcp.tool()
def dispatch_non_job_appointment_delete(appointment_id: int) -> Dict[str, Any]:
    """Delete a non-job appointment. DELETE /dispatch/v2/.../non-job-appointments/{id}"""
    return _request("DELETE", _url("dispatch", f"non-job-appointments/{appointment_id}"))

@mcp.tool()
def dispatch_technician_shifts_list(page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    """List technician shifts. GET /dispatch/v2/.../technician-shifts"""
    return _request("GET", _url("dispatch", "technician-shifts"), params={"page": page, "pageSize": page_size})

@mcp.tool()
def dispatch_technician_shift_create(technician_id: int, start: str, end: str,
    title: str = "Shift", shift_type: str = "Normal", repeat_type: str = "Never",
    note: Optional[str] = None) -> Dict[str, Any]:
    """Create a technician shift. POST /dispatch/v2/.../technician-shifts
    start/end: ISO 8601.
    shift_type: 'Normal', 'OnCall', 'TimeOff'
    repeat_type: 'Never', 'Daily', 'Weekly'"""
    body = _strip_none({"technicianIds": [technician_id], "title": title,
                         "shiftType": shift_type, "repeatType": repeat_type,
                         "start": start, "end": end, "note": note})
    return _request("POST", _url("dispatch", "technician-shifts"), json=body)

@mcp.tool()
def dispatch_technician_shift_update(shift_id: int, start: Optional[str] = None,
    end: Optional[str] = None, title: Optional[str] = None,
    shift_type: Optional[str] = None, note: Optional[str] = None) -> Dict[str, Any]:
    """Update a technician shift. PATCH /dispatch/v2/.../technician-shifts/{id}
    shift_type: 'Normal', 'OnCall', 'TimeOff'"""
    body = _strip_none({"start": start, "end": end, "title": title,
                         "shiftType": shift_type, "note": note})
    return _request("PATCH", _url("dispatch", f"technician-shifts/{shift_id}"), json=body)

@mcp.tool()
def dispatch_technician_shift_delete(shift_id: int) -> Dict[str, Any]:
    """Delete a technician shift. DELETE /dispatch/v2/.../technician-shifts/{id}"""
    return _request("DELETE", _url("dispatch", f"technician-shifts/{shift_id}"))

@mcp.tool()
def dispatch_technician_tracking_get(technician_id: int, appointment_id: int) -> Dict[str, Any]:
    """
    Get a customer-facing technician tracking URL for a specific appointment.
    GET /dispatch/v2/tenant/{tenant}/technician-tracking

    This is NOT a list — it returns a single tracking URL for one technician+appointment
    combination. The URL can be shared with the customer to show the technician's location.
    Requires OAuth scope: tn.dis.techniciantracking:r

    Args:
        technician_id:  The technician's ID (required).
        appointment_id: The appointment ID (required).

    Returns:
        {"technicianTrackingUrl": "https://..."}
    """
    return _request(
        "GET",
        _url("dispatch", "technician-tracking"),
        params={"technicianId": technician_id, "appointmentId": appointment_id},
    )

@mcp.tool()
def dispatch_zones_list(page: int = 1, page_size: int = 100) -> Dict[str, Any]:
    """List dispatch zones. GET /dispatch/v2/.../zones"""
    return _request("GET", _url("dispatch", "zones"), params={"page": page, "pageSize": page_size})

@mcp.tool()
def dispatch_capacity(start: str, end: str, business_unit_ids: Optional[list] = None) -> Dict[str, Any]:
    """Get dispatch capacity. POST /dispatch/v2/.../capacity
    start/end: ISO 8601"""
    body = _strip_none({"start": start, "end": end, "businessUnitIds": business_unit_ids})
    return _request("POST", _url("dispatch", "capacity"), json=body)


# =============================================================================
#  JPM API — additional endpoints
# =============================================================================

@mcp.tool()
def job_cancel(job_id: int, reason_id: int, memo: str = "") -> Dict[str, Any]:
    """Cancel a job. PUT /jpm/v2/.../jobs/{id}/cancel
    Both reason_id and memo are required by the ST API.
    Use job_cancel_reasons_list() to get valid reason IDs."""
    body = {"reasonId": reason_id, "memo": memo}
    return _request("PUT", _url("jpm", f"jobs/{job_id}/cancel"), json=body)

@mcp.tool()
def job_cancel_reasons_list() -> Dict[str, Any]:
    """List all job cancel reason definitions.
    GET /jpm/v2/tenant/{tenant}/job-cancel-reasons
    Requires OAuth scope: tn.jpm.jobcancelreasons:r
    Note: distinct from /jobs/cancel-reasons?ids=... (reasons on specific jobs)."""
    return _request("GET", _url("jpm", "job-cancel-reasons"))

@mcp.tool()
def job_hold_reasons_list() -> Dict[str, Any]:
    """List all job hold reason definitions.
    GET /jpm/v2/tenant/{tenant}/job-hold-reasons
    Requires OAuth scope: tn.jpm.jobholdreasons:r
    Note: distinct from /jobs/{id}/hold-reasons (hold reasons on a specific job)."""
    return _request("GET", _url("jpm", "job-hold-reasons"))

@mcp.tool()
def job_notes_list(job_id: int) -> Dict[str, Any]:
    """List notes on a job. GET /jpm/v2/.../jobs/{id}/notes"""
    return _request("GET", _url("jpm", f"jobs/{job_id}/notes"))

@mcp.tool()
def job_note_create(job_id: int, text: str, pinned_to_top: bool = False) -> Dict[str, Any]:
    """Add a note to a job. POST /jpm/v2/.../jobs/{id}/notes"""
    return _request("POST", _url("jpm", f"jobs/{job_id}/notes"),
                    json={"text": text, "pinnedToTop": pinned_to_top})

@mcp.tool()
def job_equipment_list(job_id: int) -> Dict[str, Any]:
    """List equipment on a job. GET /jpm/v2/.../jobs/{id}/equipment"""
    return _request("GET", _url("jpm", f"jobs/{job_id}/equipment"))

@mcp.tool()
def job_equipment_add(job_id: int, installed_equipment_id: int) -> Dict[str, Any]:
    """Add equipment to a job. POST /jpm/v2/.../jobs/{id}/equipment"""
    return _request("POST", _url("jpm", f"jobs/{job_id}/equipment"),
                    json={"installedEquipmentId": installed_equipment_id})

@mcp.tool()
def job_equipment_remove(job_id: int, equipment_id: int) -> Dict[str, Any]:
    """Remove equipment from a job. DELETE /jpm/v2/.../jobs/{id}/equipment/{equipmentId}"""
    return _request("DELETE", _url("jpm", f"jobs/{job_id}/equipment/{equipment_id}"))

@mcp.tool()
def job_history(job_id: int) -> Dict[str, Any]:
    """Get job history log. GET /jpm/v2/.../jobs/{id}/history"""
    return _request("GET", _url("jpm", f"jobs/{job_id}/history"))

@mcp.tool()
def job_update(job_id: int, summary: Optional[str] = None, priority: Optional[str] = None,
               job_type_id: Optional[int] = None, campaign_id: Optional[int] = None,
               custom_fields: Optional[list] = None) -> Dict[str, Any]:
    """Update a job. PATCH /jpm/v2/.../jobs/{id}"""
    body = _strip_none({"summary": summary, "priority": priority,
                         "jobTypeId": job_type_id, "campaignId": campaign_id,
                         "customFields": custom_fields})
    return _request("PATCH", _url("jpm", f"jobs/{job_id}"), json=body)

@mcp.tool()
def appointment_reschedule(appointment_id: int, start: str, end: str,
                           arrival_window_start: Optional[str] = None,
                           arrival_window_end: Optional[str] = None) -> Dict[str, Any]:
    """Reschedule an appointment. PATCH /jpm/v2/.../appointments/{id}/reschedule
    start/end: ISO 8601 datetime strings (e.g. 2026-06-10T08:00:00Z).
    arrival_window_start/end: optional — if omitted, defaults to match start/end so the
    arrival window always updates along with the appointment time."""
    body: Dict[str, Any] = {
        "start": start,
        "end":   end,
        "arrivalWindowStart": arrival_window_start if arrival_window_start else start,
        "arrivalWindowEnd":   arrival_window_end   if arrival_window_end   else end,
    }
    return _request("PATCH", _url("jpm", f"appointments/{appointment_id}/reschedule"), json=body)

@mcp.tool()
def appointment_special_instructions(appointment_id: int, instructions: str) -> Dict[str, Any]:
    """Set special instructions on an appointment. PUT /jpm/v2/.../appointments/{id}/special-instructions"""
    return _request("PUT", _url("jpm", f"appointments/{appointment_id}/special-instructions"),
                    json={"specialInstructions": instructions})

@mcp.tool()
def projects_list(page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    """List projects. GET /jpm/v2/.../projects"""
    return _request("GET", _url("jpm", "projects"), params={"page": page, "pageSize": page_size})

@mcp.tool()
def project_get(project_id: int) -> Dict[str, Any]:
    """Get a project by ID. GET /jpm/v2/.../projects/{id}"""
    return _request("GET", _url("jpm", f"projects/{project_id}"))

@mcp.tool()
def project_create(name: str, business_unit_id: int, summary: Optional[str] = None,
                   customer_id: Optional[int] = None) -> Dict[str, Any]:
    """Create a project. POST /jpm/v2/.../projects"""
    body = _strip_none({"name": name, "businessUnitId": business_unit_id,
                         "summary": summary, "customerId": customer_id})
    return _request("POST", _url("jpm", "projects"), json=body)

@mcp.tool()
def project_update(project_id: int, name: Optional[str] = None, summary: Optional[str] = None,
                   status_id: Optional[int] = None) -> Dict[str, Any]:
    """Update a project. PATCH /jpm/v2/.../projects/{id}"""
    body = _strip_none({"name": name, "summary": summary, "statusId": status_id})
    return _request("PATCH", _url("jpm", f"projects/{project_id}"), json=body)

@mcp.tool()
def project_attach_job(project_id: int, job_id: int) -> Dict[str, Any]:
    """Attach a job to a project. POST /jpm/v2/.../projects/{id}/attach-job/{jobId}"""
    return _request("POST", _url("jpm", f"projects/{project_id}/attach-job/{job_id}"))

@mcp.tool()
def project_detach_job(job_id: int) -> Dict[str, Any]:
    """Detach a job from its project. POST /jpm/v2/.../projects/detach-job/{jobId}"""
    return _request("POST", _url("jpm", f"projects/detach-job/{job_id}"))

@mcp.tool()
def project_note_create(project_id: int, text: str, pinned_to_top: bool = False) -> Dict[str, Any]:
    """Add a note to a project. POST /jpm/v2/.../projects/{id}/notes"""
    return _request("POST", _url("jpm", f"projects/{project_id}/notes"),
                    json={"text": text, "pinnedToTop": pinned_to_top})


# =============================================================================
#  INVENTORY API — additional endpoints
# =============================================================================

@mcp.tool()
def inventory_returns_list(page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    """List inventory returns. GET /inventory/v2/.../returns"""
    return _request("GET", _url("inventory", "returns"), params={"page": page, "pageSize": page_size})

@mcp.tool()
def inventory_return_create(vendor_id: int, inventory_location_id: int, job_id: Optional[int] = None,
    items: Optional[list] = None, memo: Optional[str] = None) -> Dict[str, Any]:
    """Create an inventory return. POST /inventory/v2/.../returns
    items: [{"skuId": 123, "quantity": 1, "cost": 0.0}]"""
    body = _strip_none({"vendorId": vendor_id, "inventoryLocationId": inventory_location_id,
                         "jobId": job_id, "items": items or [], "memo": memo})
    return _request("POST", _url("inventory", "returns"), json=body)

@mcp.tool()
def inventory_return_cancel(return_id: int) -> Dict[str, Any]:
    """Cancel an inventory return. PATCH /inventory/v2/.../returns/{id}/cancellation"""
    return _request("PATCH", _url("inventory", f"returns/{return_id}/cancellation"))

@mcp.tool()
def inventory_adjustments_list(page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    """List inventory adjustments. GET /inventory/v2/.../adjustments"""
    return _request("GET", _url("inventory", "adjustments"), params={"page": page, "pageSize": page_size})

@mcp.tool()
def inventory_transfers_list(page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    """List inventory transfers. GET /inventory/v2/.../transfers"""
    return _request("GET", _url("inventory", "transfers"), params={"page": page, "pageSize": page_size})

@mcp.tool()
def inventory_trucks_list(page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    """List inventory trucks. GET /inventory/v2/.../trucks"""
    return _request("GET", _url("inventory", "trucks"), params={"page": page, "pageSize": page_size})

@mcp.tool()
def inventory_truck_create(name: str, technician_id: Optional[int] = None,
    memo: Optional[str] = None) -> Dict[str, Any]:
    """Create an inventory truck. POST /inventory/v2/.../trucks"""
    body = _strip_none({"name": name, "technicianId": technician_id, "memo": memo})
    return _request("POST", _url("inventory", "trucks"), json=body)

@mcp.tool()
def inventory_return_types_list(active_only: bool = True) -> Dict[str, Any]:
    """List inventory return types. GET /inventory/v2/.../return-types

    Args:
        active_only: Required by ST API. True = active types only (default), False = all types.
    """
    return _request("GET", _url("inventory", "return-types"), params={"activeOnly": str(active_only).lower()})

@mcp.tool()
def po_requests_list(page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    """List purchase order requests. GET /inventory/v2/.../purchase-orders/requests"""
    return _request("GET", _url("inventory", "purchase-orders/requests"),
                    params={"page": page, "pageSize": page_size})

@mcp.tool()
def po_request_approve(request_id: int) -> Dict[str, Any]:
    """Approve a purchase order request. PATCH /inventory/v2/.../purchase-orders/requests/{id}/approve"""
    return _request("PATCH", _url("inventory", f"purchase-orders/requests/{request_id}/approve"))

@mcp.tool()
def po_request_reject(request_id: int, reason: Optional[str] = None) -> Dict[str, Any]:
    """Reject a purchase order request. PATCH /inventory/v2/.../purchase-orders/requests/{id}/reject"""
    body = _strip_none({"reason": reason})
    return _request("PATCH", _url("inventory", f"purchase-orders/requests/{request_id}/reject"), json=body)

@mcp.tool()
def inventory_templates_list() -> Dict[str, Any]:
    """List inventory templates. GET /inventory/v2/.../inventory-templates"""
    return _request("GET", _url("inventory", "inventory-templates"))

@mcp.tool()
def vendor_get(vendor_id: int) -> Dict[str, Any]:
    """Get a vendor by ID. GET /inventory/v2/.../vendors/{id}"""
    return _request("GET", _url("inventory", f"vendors/{vendor_id}"))

@mcp.tool()
def vendor_create(name: str, address: Optional[dict] = None, phone: Optional[str] = None,
    email: Optional[str] = None, memo: Optional[str] = None) -> Dict[str, Any]:
    """Create a vendor. POST /inventory/v2/.../vendors
    address: {"street": "...", "city": "...", "state": "...", "zip": "...", "country": "USA"}"""
    body = _strip_none({"name": name, "memo": memo,
                         "address": address,
                         "contactInfo": _strip_none({"phone": phone, "email": email}) if phone or email else None})
    return _request("POST", _url("inventory", "vendors"), json=body)

@mcp.tool()
def vendor_update(vendor_id: int, name: Optional[str] = None, memo: Optional[str] = None,
    phone: Optional[str] = None, email: Optional[str] = None) -> Dict[str, Any]:
    """Update a vendor. PATCH /inventory/v2/.../vendors/{id}"""
    body = _strip_none({"name": name, "memo": memo,
                         "contactInfo": _strip_none({"phone": phone, "email": email}) if phone or email else None})
    return _request("PATCH", _url("inventory", f"vendors/{vendor_id}"), json=body)


# =============================================================================
#  PRICEBOOK API
# =============================================================================

@mcp.tool()
def pricebook_services_list(page: int = 1, page_size: int = 200,
    active: Optional[bool] = True) -> Dict[str, Any]:
    """List pricebook services. GET /pricebook/v2/.../services"""
    return _request("GET", _url("pricebook", "services"),
                    params=_strip_none({"page": page, "pageSize": page_size, "active": active}))

@mcp.tool()
def pricebook_service_get(service_id: int) -> Dict[str, Any]:
    """Get a pricebook service by ID. GET /pricebook/v2/.../services/{id}"""
    return _request("GET", _url("pricebook", f"services/{service_id}"))

@mcp.tool()
def pricebook_service_create(code: str, display_name: str, price: float,
    description: Optional[str] = None, hours: Optional[float] = None,
    is_labor: bool = False, taxable: bool = False,
    category_ids: Optional[list] = None, member_price: float = 0.0,
    account: str = "SALES INCOME") -> Dict[str, Any]:
    """Create a pricebook service. POST /pricebook/v2/.../services
    account: income account name, e.g. 'SALES INCOME' or 'Revenue' (required by ST)."""
    body = {"code": code, "displayName": display_name,
            "description": description or "",
            "price": price, "memberPrice": member_price,
            "hours": hours or 0, "isLabor": is_labor, "taxable": taxable,
            "account": account,
            "categories": [{"id": c} for c in (category_ids or [])]}
    return _request("POST", _url("pricebook", "services"), json=body)

@mcp.tool()
def pricebook_service_update(service_id: int, display_name: Optional[str] = None,
    price: Optional[float] = None, description: Optional[str] = None,
    hours: Optional[float] = None, active: Optional[bool] = None,
    member_price: Optional[float] = None) -> Dict[str, Any]:
    """Update a pricebook service. PATCH /pricebook/v2/.../services/{id}"""
    body = _strip_none({"displayName": display_name, "price": price, "description": description,
                         "hours": hours, "active": active, "memberPrice": member_price})
    return _request("PATCH", _url("pricebook", f"services/{service_id}"), json=body)

@mcp.tool()
def pricebook_service_delete(service_id: int) -> Dict[str, Any]:
    """Delete/deactivate a pricebook service. DELETE /pricebook/v2/.../services/{id}"""
    return _request("DELETE", _url("pricebook", f"services/{service_id}"))

@mcp.tool()
def pricebook_materials_list(page: int = 1, page_size: int = 200,
    active: Optional[bool] = True) -> Dict[str, Any]:
    """List pricebook materials. GET /pricebook/v2/.../materials"""
    return _request("GET", _url("pricebook", "materials"),
                    params=_strip_none({"page": page, "pageSize": page_size, "active": active}))

@mcp.tool()
def pricebook_material_get(material_id: int) -> Dict[str, Any]:
    """Get a pricebook material by ID. GET /pricebook/v2/.../materials/{id}"""
    return _request("GET", _url("pricebook", f"materials/{material_id}"))

@mcp.tool()
def pricebook_material_create(code: str, display_name: str, price: float, cost: float = 0.0,
    description: Optional[str] = None, taxable: bool = True,
    category_ids: Optional[list] = None, member_price: float = 0.0,
    account: str = "SALES INCOME") -> Dict[str, Any]:
    """Create a pricebook material. POST /pricebook/v2/.../materials
    account: income account name, e.g. 'SALES INCOME' or 'Revenue' (required by ST)."""
    body = {"code": code, "displayName": display_name,
            "description": description or "",
            "price": price, "cost": cost, "memberPrice": member_price,
            "taxable": taxable, "account": account,
            "categories": [{"id": c} for c in (category_ids or [])]}
    return _request("POST", _url("pricebook", "materials"), json=body)

@mcp.tool()
def pricebook_material_update(material_id: int, display_name: Optional[str] = None,
    price: Optional[float] = None, cost: Optional[float] = None,
    description: Optional[str] = None, active: Optional[bool] = None,
    member_price: Optional[float] = None) -> Dict[str, Any]:
    """Update a pricebook material. PATCH /pricebook/v2/.../materials/{id}"""
    body = _strip_none({"displayName": display_name, "price": price, "cost": cost,
                         "description": description, "active": active, "memberPrice": member_price})
    return _request("PATCH", _url("pricebook", f"materials/{material_id}"), json=body)

@mcp.tool()
def pricebook_material_delete(material_id: int) -> Dict[str, Any]:
    """Delete/deactivate a pricebook material. DELETE /pricebook/v2/.../materials/{id}"""
    return _request("DELETE", _url("pricebook", f"materials/{material_id}"))

@mcp.tool()
def pricebook_equipment_list(page: int = 1, page_size: int = 200,
    active: Optional[bool] = True) -> Dict[str, Any]:
    """List pricebook equipment. GET /pricebook/v2/.../equipment"""
    return _request("GET", _url("pricebook", "equipment"),
                    params=_strip_none({"page": page, "pageSize": page_size, "active": active}))

@mcp.tool()
def pricebook_equipment_get(equipment_id: int) -> Dict[str, Any]:
    """Get a pricebook equipment item by ID. GET /pricebook/v2/.../equipment/{id}"""
    return _request("GET", _url("pricebook", f"equipment/{equipment_id}"))

@mcp.tool()
def pricebook_equipment_create(code: str, display_name: str, price: float, cost: float = 0.0,
    description: Optional[str] = None, taxable: bool = True,
    category_ids: Optional[list] = None,
    account: str = "SALES INCOME") -> Dict[str, Any]:
    """Create a pricebook equipment item. POST /pricebook/v2/.../equipment
    account: income account name, e.g. 'SALES INCOME' or 'Revenue' (required by ST)."""
    body = {"code": code, "displayName": display_name,
            "description": description or "",
            "price": price, "cost": cost, "taxable": taxable,
            "account": account,
            "categories": [{"id": c} for c in (category_ids or [])]}
    return _request("POST", _url("pricebook", "equipment"), json=body)

@mcp.tool()
def pricebook_equipment_update(equipment_id: int, display_name: Optional[str] = None,
    price: Optional[float] = None, cost: Optional[float] = None,
    description: Optional[str] = None, active: Optional[bool] = None) -> Dict[str, Any]:
    """Update a pricebook equipment item. PATCH /pricebook/v2/.../equipment/{id}"""
    body = _strip_none({"displayName": display_name, "price": price, "cost": cost,
                         "description": description, "active": active})
    return _request("PATCH", _url("pricebook", f"equipment/{equipment_id}"), json=body)

@mcp.tool()
def pricebook_equipment_delete(equipment_id: int) -> Dict[str, Any]:
    """Delete/deactivate a pricebook equipment item. DELETE /pricebook/v2/.../equipment/{id}"""
    return _request("DELETE", _url("pricebook", f"equipment/{equipment_id}"))

@mcp.tool()
def pricebook_categories_list(page: int = 1, page_size: int = 200) -> Dict[str, Any]:
    """List pricebook categories. GET /pricebook/v2/.../categories"""
    return _request("GET", _url("pricebook", "categories"), params={"page": page, "pageSize": page_size})

@mcp.tool()
def pricebook_category_create(name: str, parent_id: Optional[int] = None) -> Dict[str, Any]:
    """Create a pricebook category. POST /pricebook/v2/.../categories"""
    body = _strip_none({"name": name, "parentId": parent_id})
    return _request("POST", _url("pricebook", "categories"), json=body)

@mcp.tool()
def pricebook_category_update(category_id: int, name: Optional[str] = None,
    active: Optional[bool] = None) -> Dict[str, Any]:
    """Update a pricebook category. PATCH /pricebook/v2/.../categories/{id}"""
    body = _strip_none({"name": name, "active": active})
    return _request("PATCH", _url("pricebook", f"categories/{category_id}"), json=body)

@mcp.tool()
def pricebook_discounts_and_fees_list(page: int = 1, page_size: int = 100) -> Dict[str, Any]:
    """List pricebook discounts and fees. GET /pricebook/v2/.../discounts-and-fees"""
    return _request("GET", _url("pricebook", "discounts-and-fees"),
                    params={"page": page, "pageSize": page_size})

@mcp.tool()
def pricebook_bulk_update(items: list) -> Dict[str, Any]:
    """Bulk update pricebook items. PATCH /pricebook/v2/.../pricebook
    items: list of item dicts with id and fields to update."""
    return _request("PATCH", _url("pricebook", "pricebook"), json={"items": items})


# =============================================================================
#  MEMBERSHIPS API
# =============================================================================

@mcp.tool()
def memberships_list(page: int = 1, page_size: int = 50,
    customer_id: Optional[int] = None, active: Optional[bool] = None) -> Dict[str, Any]:
    """List customer memberships. GET /memberships/v2/.../memberships"""
    return _request("GET", _url("memberships", "memberships"),
                    params=_strip_none({"page": page, "pageSize": page_size,
                                        "customerId": customer_id, "active": active}))

@mcp.tool()
def membership_get(membership_id: int) -> Dict[str, Any]:
    """Get a membership by ID. GET /memberships/v2/.../memberships/{id}"""
    return _request("GET", _url("memberships", f"memberships/{membership_id}"))

@mcp.tool()
def membership_update(membership_id: int, next_scheduled_billing_date: Optional[str] = None,
    follow_up_date: Optional[str] = None, memo: Optional[str] = None) -> Dict[str, Any]:
    """Update a membership. PATCH /memberships/v2/.../memberships/{id}"""
    body = _strip_none({"nextScheduledBillingDate": next_scheduled_billing_date,
                         "followUpDate": follow_up_date, "memo": memo})
    return _request("PATCH", _url("memberships", f"memberships/{membership_id}"), json=body)

@mcp.tool()
def membership_sell(customer_id: int, membership_type_id: int, location_id: Optional[int] = None,
    start_date: Optional[str] = None, from_job_id: Optional[int] = None) -> Dict[str, Any]:
    """Sell/create a membership for a customer. POST /memberships/v2/.../memberships/sale"""
    body = _strip_none({"customerId": customer_id, "membershipTypeId": membership_type_id,
                         "locationId": location_id, "startDate": start_date, "fromJobId": from_job_id})
    return _request("POST", _url("memberships", "memberships/sale"), json=body)

@mcp.tool()
def membership_status_changes(membership_id: int) -> Dict[str, Any]:
    """Get status change history for a membership. GET /memberships/v2/.../memberships/{id}/status-changes"""
    return _request("GET", _url("memberships", f"memberships/{membership_id}/status-changes"))

@mcp.tool()
def membership_types_list(page: int = 1, page_size: int = 100,
    active: Optional[bool] = True) -> Dict[str, Any]:
    """List membership types. GET /memberships/v2/.../membership-types"""
    return _request("GET", _url("memberships", "membership-types"),
                    params=_strip_none({"page": page, "pageSize": page_size, "active": active}))

@mcp.tool()
def membership_type_get(type_id: int) -> Dict[str, Any]:
    """Get a membership type by ID. GET /memberships/v2/.../membership-types/{id}"""
    return _request("GET", _url("memberships", f"membership-types/{type_id}"))

@mcp.tool()
def membership_type_discounts(type_id: int) -> Dict[str, Any]:
    """Get discounts for a membership type. GET /memberships/v2/.../membership-types/{id}/discounts"""
    return _request("GET", _url("memberships", f"membership-types/{type_id}/discounts"))

@mcp.tool()
def recurring_services_list(page: int = 1, page_size: int = 50,
    customer_id: Optional[int] = None) -> Dict[str, Any]:
    """List recurring services. GET /memberships/v2/.../recurring-services"""
    return _request("GET", _url("memberships", "recurring-services"),
                    params=_strip_none({"page": page, "pageSize": page_size, "customerId": customer_id}))

@mcp.tool()
def recurring_service_get(service_id: int) -> Dict[str, Any]:
    """Get a recurring service by ID. GET /memberships/v2/.../recurring-services/{id}"""
    return _request("GET", _url("memberships", f"recurring-services/{service_id}"))

@mcp.tool()
def recurring_service_update(service_id: int, next_visit_date: Optional[str] = None,
    memo: Optional[str] = None) -> Dict[str, Any]:
    """Update a recurring service. PATCH /memberships/v2/.../recurring-services/{id}"""
    body = _strip_none({"nextVisitDate": next_visit_date, "memo": memo})
    return _request("PATCH", _url("memberships", f"recurring-services/{service_id}"), json=body)

@mcp.tool()
def recurring_service_events_list(page: int = 1, page_size: int = 50,
    customer_id: Optional[int] = None) -> Dict[str, Any]:
    """List recurring service events. GET /memberships/v2/.../recurring-service-events"""
    return _request("GET", _url("memberships", "recurring-service-events"),
                    params=_strip_none({"page": page, "pageSize": page_size, "customerId": customer_id}))

@mcp.tool()
def recurring_service_event_complete(event_id: int) -> Dict[str, Any]:
    """Mark a recurring service event complete. POST /memberships/v2/.../recurring-service-events/{id}/mark-complete"""
    return _request("POST", _url("memberships", f"recurring-service-events/{event_id}/mark-complete"))

@mcp.tool()
def recurring_service_event_incomplete(event_id: int) -> Dict[str, Any]:
    """Mark a recurring service event incomplete. POST /memberships/v2/.../recurring-service-events/{id}/mark-incomplete"""
    return _request("POST", _url("memberships", f"recurring-service-events/{event_id}/mark-incomplete"))

@mcp.tool()
def invoice_templates_list(ids: str) -> Dict[str, Any]:
    """Get membership invoice templates by IDs (up to 50, comma-separated).
    GET /memberships/v2/tenant/{tenant}/invoice-templates
    Requires OAuth scope: tn.mem.invoicetemplates:r

    IMPORTANT: The ST Memberships API does NOT allow enumerating all invoice templates.
    You must provide specific IDs obtained from a CustomerMembership (billingTemplateId)
    or LocationRecurringService (invoiceTemplateId). Use export_invoice_templates for
    full enumeration via the export feed.

    Args:
        ids: Comma-separated invoice template IDs, e.g. "123,456,789" (max 50)
    """
    return _request("GET", _url("memberships", "invoice-templates"),
                    params={"ids": ids})


# =============================================================================
#  FORMS API
# =============================================================================

@mcp.tool()
def forms_list(page: int = 1, page_size: int = 50) -> Dict[str, Any]:
    """List forms. GET /forms/v2/.../forms"""
    return _request("GET", _url("forms", "forms"), params={"page": page, "pageSize": page_size})

@mcp.tool()
def form_submissions_list(page: int = 1, page_size: int = 50,
    job_id: Optional[int] = None, form_id: Optional[int] = None) -> Dict[str, Any]:
    """List form submissions. GET /forms/v2/.../submissions"""
    return _request("GET", _url("forms", "submissions"),
                    params=_strip_none({"page": page, "pageSize": page_size,
                                        "jobId": job_id, "formId": form_id}))

@mcp.tool()
def job_attachments_list(job_id: int) -> Dict[str, Any]:
    """List attachments on a job. GET /forms/v2/.../jobs/{id}/attachments"""
    return _request("GET", _url("forms", f"jobs/{job_id}/attachments"))

@mcp.tool()
def job_attachment_create(job_id: int, url: str, name: Optional[str] = None,
    alias: Optional[str] = None) -> Dict[str, Any]:
    """Add a URL attachment to a job. POST /forms/v2/.../jobs/{id}/attachments"""
    body = _strip_none({"url": url, "name": name, "alias": alias})
    return _request("POST", _url("forms", f"jobs/{job_id}/attachments"), json=body)


# =============================================================================
#  SERVICE AGREEMENTS API
# =============================================================================

@mcp.tool()
def service_agreements_list(page: int = 1, page_size: int = 50,
    customer_id: Optional[int] = None, status: Optional[str] = None,
    created_on_or_after: Optional[str] = None, modified_on_or_after: Optional[str] = None) -> Dict[str, Any]:
    """List service agreements. GET /service-agreements/v2/.../service-agreements
    status: Draft, Sent, Rejected, Accepted, Activated, Canceled, Expired, AutoRenew"""
    return _request("GET", _url("service-agreements", "service-agreements"),
                    params=_strip_none({"page": page, "pageSize": page_size,
                                        "customerIds": customer_id, "status": status,
                                        "createdOnOrAfter": created_on_or_after,
                                        "modifiedOnOrAfter": modified_on_or_after}))

@mcp.tool()
def service_agreement_get(agreement_id: int) -> Dict[str, Any]:
    """Get a service agreement by ID. GET /service-agreements/v2/.../service-agreements/{id}"""
    return _request("GET", _url("service-agreements", f"service-agreements/{agreement_id}"))


# -----------------------------------------------------------------------------
#  Entry point
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
