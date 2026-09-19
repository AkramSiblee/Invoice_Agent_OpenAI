"""
Reads and writes this agent's OWN Invoice_Log and Vendor_Master Google
Sheets, kept in a dedicated "Agent Data" subfolder inside the watched
'Invoice_Automation' Drive folder (DRIVE_WATCH_FOLDER_ID) — schema-matched
to the Accounts Payable Agent's files (references/sheet_schema.md) so rows
can be copied across by a human, but a SEPARATE, invoice-agent-owned copy.

This is deliberate: an earlier version of this agent wrote directly into
the AP Agent's own shared Drive file, and a row-count mistake during
cleanup deleted one of that agent's real rows. This module must never open
a spreadsheet other than the two it owns in the Agent Data folder — see
references/sheet_schema.md for the full incident note.

intake_drive.py already excludes native Google files (SUPPORTED_MIMES)
from its walk of DRIVE_WATCH_FOLDER_ID, so these two sheets living inside
that same folder tree is safe — they're never picked up as invoices to
extract.

Every write is by COLUMN NAME against the sheet's actual header row, not a
hardcoded position.
"""
import string
from datetime import date, datetime

from googleapiclient.discovery import build

from auth import get_credentials
from config import DRIVE_WATCH_FOLDER_ID, require

FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
AGENT_DATA_FOLDER_NAME = "Agent Data"
INVOICE_LOG_NAME = "Invoice_Log"
VENDOR_MASTER_NAME = "Vendor_Master"
DEFAULT_TAB = "Sheet1"

# The AP Agent's own required columns (accounts-payable-agent/scripts/excel_loader.py)
# plus this agent's own additions — see references/sheet_schema.md for what
# each one means and which side reads it.
INVOICE_LOG_COLUMNS = [
    "invoice_number", "vendor_id", "vendor_name", "po_number", "po_line",
    "category", "qty_invoiced", "unit_price", "total", "currency", "invoice_date",
    "subtotal", "tax", "date_received", "logged_at", "source", "file_name",
    "line_items", "review_status", "issue", "approve_vendor",
]

# The AP Agent's own Vendor_Master schema, used as-is.
VENDOR_MASTER_COLUMNS = [
    "vendor_id", "vendor_name", "aliases", "category", "country", "tax_id_type",
    "tax_id", "default_currency", "payment_terms", "criticality", "status",
    "bank_details_last_changed", "bank_details_last_verified",
    "date_onboarded", "onboarded_by",
]

# Memoized within one process run (main.py runs once per invocation) so a
# batch of many invoices doesn't re-search Drive for the same folder/sheet
# on every single row — same "load once" principle as existing_rows in
# main.py.
_ids_cache: dict = {}


def _drive():
    return build("drive", "v3", credentials=get_credentials())


def _sheets():
    return build("sheets", "v4", credentials=get_credentials())


def _find_child(drive, parent_id: str, name: str, mime: str) -> str | None:
    q = (
        f"'{parent_id}' in parents and name = '{name}' "
        f"and mimeType = '{mime}' and trashed = false"
    )
    resp = drive.files().list(q=q, fields="files(id, name)", pageSize=10).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _get_agent_data_folder_id(drive) -> str:
    if "folder" in _ids_cache:
        return _ids_cache["folder"]
    require(DRIVE_WATCH_FOLDER_ID, "DRIVE_WATCH_FOLDER_ID")
    folder_id = _find_child(drive, DRIVE_WATCH_FOLDER_ID, AGENT_DATA_FOLDER_NAME, FOLDER_MIME)
    if not folder_id:
        folder = drive.files().create(
            body={
                "name": AGENT_DATA_FOLDER_NAME,
                "mimeType": FOLDER_MIME,
                "parents": [DRIVE_WATCH_FOLDER_ID],
            },
            fields="id",
        ).execute()
        folder_id = folder["id"]
    _ids_cache["folder"] = folder_id
    return folder_id


def _get_or_create_sheet(name: str, header: list[str]) -> str:
    """Finds this agent's own sheet by name inside the Agent Data folder,
    creating it (with just the header row) the first time it's needed.
    Unlike the AP Agent's files, there's no pre-existing data here to be
    careful around."""
    cache_key = f"sheet:{name}"
    if cache_key in _ids_cache:
        return _ids_cache[cache_key]

    drive = _drive()
    folder_id = _get_agent_data_folder_id(drive)
    sheet_id = _find_child(drive, folder_id, name, SHEET_MIME)

    if not sheet_id:
        spreadsheet = _sheets().spreadsheets().create(
            body={"properties": {"title": name}}
        ).execute()
        sheet_id = spreadsheet["spreadsheetId"]

        # A freshly created spreadsheet lands loose in "My Drive" — move it
        # into the Agent Data folder instead of leaving it there.
        file = drive.files().get(fileId=sheet_id, fields="parents").execute()
        drive.files().update(
            fileId=sheet_id,
            addParents=folder_id,
            removeParents=",".join(file.get("parents", [])),
            fields="id, parents",
        ).execute()

        _sheets().spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"{DEFAULT_TAB}!A1",
            valueInputOption="RAW", body={"values": [header]},
        ).execute()

    _ids_cache[cache_key] = sheet_id
    return sheet_id


def _invoice_log_id() -> str:
    return _get_or_create_sheet(INVOICE_LOG_NAME, INVOICE_LOG_COLUMNS)


def _vendor_master_id() -> str:
    return _get_or_create_sheet(VENDOR_MASTER_NAME, VENDOR_MASTER_COLUMNS)


def get_invoice_log_url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{_invoice_log_id()}/edit"


def _col_letter(index: int) -> str:
    """1-based column index -> A1-notation letters (1 -> A, 27 -> AA)."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = string.ascii_uppercase[remainder] + letters
    return letters


def _format_amount(amount) -> str:
    amount = float(amount or 0)
    return f"-${abs(amount):.2f}" if amount < 0 else f"${amount:.2f}"


def format_line_items(line_items: list[dict]) -> str:
    """Plain-text rendering, e.g. 'Widget ($12.50); Gadget (-$2.00)' — not
    JSON, so a non-technical reader doesn't have to parse braces and quotes.
    This is the only place a mixed-price receipt's itemization survives:
    qty_invoiced/unit_price stay blank unless the lines confirm one pair."""
    if not line_items:
        return ""
    return "; ".join(
        f"{item.get('description', '') or '(no description)'} ({_format_amount(item.get('amount', 0))})"
        for item in line_items
    )


def confirmed_qty_and_price(line_items: list[dict], subtotal) -> tuple:
    """(qty_invoiced, unit_price) for the log row, or ("", "") when the
    document doesn't confirm a single pair — a blank beats an invented value.

    Confirmed means every priced line carries the same unit price, and
    total quantity x that price reproduces both the sum of the line amounts
    and the subtotal (within $0.02). One line qualifies on its own; two
    44 SF lines at $2.25 collapse to 88 x $2.25. Zero-amount lines (terms,
    notes and disclaimers some invoices print as $0.00 rows) carry no price
    and are ignored. Anything else — mixed prices, a missing quantity or
    price, amounts that don't reconcile — stays blank; `line_items` still
    holds the full itemization."""
    try:
        line_items = [item for item in line_items or [] if float(item.get("amount") or 0) != 0]
    except (AttributeError, TypeError, ValueError):
        return "", ""
    if not line_items:
        return "", ""
    try:
        quantities = [float(item["quantity"]) for item in line_items]
        prices = [float(item["unit_price"]) for item in line_items]
        amounts = [float(item["amount"]) for item in line_items]
        subtotal = float(subtotal)
    except (KeyError, TypeError, ValueError):
        return "", ""
    if any(q <= 0 for q in quantities) or any(p <= 0 for p in prices):
        return "", ""
    if max(prices) - min(prices) > 0.005:
        return "", ""

    qty, price, amount_sum = sum(quantities), prices[0], sum(amounts)
    if abs(qty * price - amount_sum) > 0.02 or abs(amount_sum - subtotal) > 0.02:
        return "", ""
    return (int(qty) if qty == int(qty) else qty), price


def _read_records(spreadsheet_id: str, header: list[str]) -> list[dict]:
    """Returns one dict per data row, keyed by the sheet's actual header
    row. A blank cell reads back as None, matching dict.get() semantics
    used throughout the pipeline."""
    last_col = _col_letter(len(header))
    resp = _sheets().spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{DEFAULT_TAB}!A1:{last_col}100000",
    ).execute()
    rows = resp.get("values", [])
    if not rows:
        return []
    file_header = rows[0]
    records = []
    for i, row in enumerate(rows[1:], start=2):  # row 1 is the header
        record = {file_header[j]: (row[j] if j < len(row) else None) for j in range(len(file_header))}
        record["_row_number"] = i
        records.append(record)
    return records


def _append_by_name(spreadsheet_id: str, header: list[str], values: dict) -> None:
    row = [values.get(col, "") for col in header]
    _sheets().spreadsheets().values().append(
        spreadsheetId=spreadsheet_id, range=f"{DEFAULT_TAB}!A1",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def _update_cell(spreadsheet_id: str, header: list[str], row_number: int, column: str, value) -> None:
    col_letter = _col_letter(header.index(column) + 1)
    _sheets().spreadsheets().values().update(
        spreadsheetId=spreadsheet_id, range=f"{DEFAULT_TAB}!{col_letter}{row_number}",
        valueInputOption="USER_ENTERED", body={"values": [[value]]},
    ).execute()


def get_master_vendors() -> list[dict]:
    return _read_records(_vendor_master_id(), VENDOR_MASTER_COLUMNS)


def _next_vendor_id(existing: list[dict]) -> str:
    numbers = [
        int(str(v["vendor_id"])[2:])
        for v in existing
        if str(v.get("vendor_id") or "").startswith("V-") and str(v["vendor_id"])[2:].isdigit()
    ]
    return f"V-{(max(numbers) + 1) if numbers else 1001:04d}"


def add_vendor(vendor_name: str, aliases: str = "") -> str:
    """Appends a MINIMAL vendor row: vendor_id, vendor_name, aliases, and
    status='Pending'. Every other column (country, tax_id, payment_terms,
    criticality, bank details...) is left blank — Claude has no way to know
    these from an invoice. If this sheet's rows are ever copied into the AP
    Agent's real Vendor_Master.xlsx, its own status gate already hard-blocks
    anything that isn't 'Active', so a vendor sits inert until a human
    finishes onboarding it directly.

    Returns the newly assigned vendor_id.
    """
    existing = get_master_vendors()
    vendor_id = _next_vendor_id(existing)

    values = {
        "vendor_id": vendor_id,
        "vendor_name": vendor_name,
        "aliases": aliases,
        "status": "Pending",
        "date_onboarded": date.today().isoformat(),
        "onboarded_by": "invoice-agent (pending human completion)",
    }
    _append_by_name(_vendor_master_id(), VENDOR_MASTER_COLUMNS, values)
    return vendor_id


def get_invoice_log_rows() -> list[dict]:
    return _read_records(_invoice_log_id(), INVOICE_LOG_COLUMNS)


def append_invoice_row(record: dict, source: str, status: str, issues: list[str]) -> None:
    """Appends a row to Invoice_Log in the AP-Agent-matching schema.

    qty_invoiced/unit_price are filled only when the line items confirm them
    (see confirmed_qty_and_price) and are left blank otherwise, never
    defaulted to 1 x subtotal. The full itemization is preserved in the
    line_items column.
    """
    qty_invoiced, unit_price = confirmed_qty_and_price(record.get("line_items"), record.get("subtotal"))
    values = {
        "invoice_number": record.get("invoice_number") or "",
        "vendor_id": record.get("vendor_id") or "",
        "vendor_name": record.get("vendor", ""),
        "po_number": record.get("po_number") or "",
        "po_line": record.get("po_line") or "",
        "category": record.get("category") or "",
        "qty_invoiced": qty_invoiced,
        "unit_price": unit_price,
        "total": record.get("total", 0),
        "currency": record.get("currency") or "",
        "invoice_date": record.get("invoice_date") or "",
        "subtotal": record.get("subtotal", ""),
        "tax": record.get("tax", ""),
        "date_received": date.today().isoformat(),
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "file_name": record.get("source_file", ""),
        "line_items": format_line_items(record.get("line_items", [])),
        "review_status": status,
        "issue": "; ".join(issues),
        "approve_vendor": "",
    }
    _append_by_name(_invoice_log_id(), INVOICE_LOG_COLUMNS, values)


def get_rows_pending_approval() -> list[dict]:
    """Rows still needs_review where a human has set approve_vendor = TRUE."""
    return [
        r for r in get_invoice_log_rows()
        if r.get("review_status") == "needs_review"
        and str(r.get("approve_vendor") or "").strip().upper() == "TRUE"
    ]


def update_row_status(row_number: int, status: str, issues: list[str]) -> None:
    sheet_id = _invoice_log_id()
    _update_cell(sheet_id, INVOICE_LOG_COLUMNS, row_number, "review_status", status)
    _update_cell(sheet_id, INVOICE_LOG_COLUMNS, row_number, "issue", "; ".join(issues))


def update_row_vendor_id(row_number: int, vendor_id: str) -> None:
    """Called right after a pending vendor is approved and assigned an id,
    so the invoice row that triggered the approval carries the real
    vendor_id instead of being left blank."""
    _update_cell(_invoice_log_id(), INVOICE_LOG_COLUMNS, row_number, "vendor_id", vendor_id)
