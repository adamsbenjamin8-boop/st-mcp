# servicetitan_writer.py — Fix Log

## Session: 2026-05-29 / 2026-05-30

### Fix 1 — Empty response / HTTP 204 handler in `_request()`

**Problem:** ServiceTitan returns HTTP 204 No Content for successful PATCH/DELETE calls, and sometimes 200/201 with no body. FastMCP/Pydantic would crash trying to parse an empty body.

**Fix:** Added guards at the top of the response-handling block in `_request()`:
```python
if response.status_code == 204:
    return {"success": True, "status_code": 204}

if not response.content:
    return {"success": True, "status_code": response.status_code}
```

Also improved error detail for "Unable to match incoming request to an operation" — this error means a **missing write API scope** in the ST Developer portal (jpm:write, accounting:write, payroll:write, inventory:write). The error response now includes a `_hint` explaining this.

---

### Fix 2 — `po_create` missing required fields

**Problem:** `po_create` was returning HTTP 400 because the ServiceTitan PO creation endpoint requires `inventoryLocationId`, `impactsTechnicianPayroll`, and `shipping` fields.

**Fix:** Added these three parameters to `po_create()` signature and body dict:
- `inventory_location_id: Optional[int]`
- `impacts_technician_payroll: Optional[bool]`
- `shipping: Optional[float]`

Use `inventory_locations_list()` to find valid `inventory_location_id` values.

---

### Fix 3 — `inventory_locations_list` wrong URL path

**Problem:** Function was calling `_url("inventory", "locations")` which returns 404. The correct ST API path is `/inventory/v2/tenant/{tenant}/inventory-locations`.

**Fix:** Changed to `_url("inventory", "inventory-locations")`.

---

### Fix 4 — `po_create_smart` missing required fields

**Problem:** Same root cause as Fix 2 — the smart PO creation helper didn't pass `inventoryLocationId`, `impactsTechnicianPayroll`, or `shipping` through to the underlying API call.

**Fix:** Added three optional parameters to `po_create_smart()` signature:
- `inventory_location_id: Optional[int]`
- `impacts_technician_payroll: Optional[bool]`
- `shipping: Optional[float]`

And included them in the Step 4 body dict.

---

---

### Fix 5 — Estimate functions: wrong API module (`jpm` → `sales`) and wrong HTTP methods

**Problem:** ALL estimate write operations were routing to `/jpm/v2/...` but the ServiceTitan Estimates API lives under `/sales/v2/...`. Additionally, sub-resource operations (`sell`, `unsell`, `dismiss`, `items`) use **PUT** not POST/PATCH.

**Confirmed from ST Developer portal API docs:**
- `PUT /sales/v2/tenant/{t}/estimates/{id}` — update estimate header
- `PUT /sales/v2/tenant/{t}/estimates/{id}/items` — add/update item
- `PUT /sales/v2/tenant/{t}/estimates/{id}/items/{itemId}` — update specific item
- `DELETE /sales/v2/tenant/{t}/estimates/{id}/items/{itemId}` — delete item
- `PUT /sales/v2/tenant/{t}/estimates/{id}/sell` — sell estimate
- `PUT /sales/v2/tenant/{t}/estimates/{id}/unsell` — unsell estimate
- `PUT /sales/v2/tenant/{t}/estimates/{id}/dismiss` — dismiss estimate

**Functions fixed:** `estimate_update`, `estimate_add_item`, `estimate_update_item`, `estimate_delete_item`, `estimate_sell`, `estimate_unsell`, `estimate_dismiss`

---

### Fix 6 — `invoice_add_item` wrong HTTP method (`POST` → `PATCH`)

**Problem:** `invoice_add_item` was calling `POST invoices/{id}/items` but the Accounting API uses `PATCH invoices/{invoiceId}/items` for adding/updating invoice items.

**Fix:** Changed HTTP method from `POST` to `PATCH` in `invoice_add_item`.

---

## Monday Checklist

- [ ] Restart `servicetitan_writer.py` connector so new functions load (job_create, job_types_list, all fixes above)
- [ ] In ST Developer portal (developer.servicetitan.io): enable write API scopes on the app:
  - `jpm:write`
  - `accounting:write`
  - `payroll:write`
  - `inventory:write`
  - This is the **likely root cause** of all 404 "Unable to match" errors on write endpoints (estimates, invoices, timesheets, POs)
- [ ] Test `job_create` with the new writer function (Ben Adams / 47 Mascuppic Trail / Com HVAC Install / "Ben Claude Test")
- [ ] Test `invoice_add_item` and `invoice_update_item` against an **open current job** (not archived 2020 records)
- [ ] Test `estimate_sell`, `estimate_unsell`, `estimate_dismiss` on a fresh estimate
- [ ] Test PO creation end-to-end with `po_create_smart` (use `inventory_locations_list()` first to get a valid location ID)
- [ ] Confirm `timesheet_create` works after write scopes enabled

---

## Known Issues (Not Code Bugs)

- **Write API 404s**: "Unable to match incoming request to an operation" on all write sub-resource paths is almost certainly missing write scopes in ST Developer portal — NOT a bug in this file. Enable scopes Monday.
- **Non-job timesheets have no job IDs**: This is expected ST behavior. Job time entries live on job timesheets separately from clock-in/out records.
- **Approval popups in Claude**: settings.local.json has been written to Downloads, Documents, and ST folders with all tool names explicitly listed. New task sessions should skip approvals. Existing sessions need one approval per session.
