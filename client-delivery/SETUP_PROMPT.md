You are setting up a working piece of software for me in this empty folder:
an **Invoice Processing Agent**. It watches Gmail and/or a Google Drive
folder for incoming invoices/receipts, reads each one with OpenAI, logs it
to a Google Sheet, validates it (vendor match, math check, duplicate check,
etc.), and emails me when something needs a human look — never silently
guessing.

Follow the steps below **in order**. Do not skip the interview step, do not
invent values for anything I need to provide (API keys, folder IDs, my
email), and do not run `python scripts/main.py` for real until setup is
fully complete and I've confirmed I'm ready. Ask me questions **one at a
time** and wait for my reply before asking the next one or writing any
config value — don't dump a list of questions at once.

---

## Step 1 — Create the file structure

Create every file below, at the exact path shown, with the exact content
shown. These are complete, working files — write them verbatim, don't
paraphrase or "improve" them.

#### `requirements.txt`
```text
openai>=1.55.0
google-api-python-client>=2.100.0
google-auth-httplib2>=0.2.0
google-auth-oauthlib>=1.2.0
python-dotenv>=1.0.0
```

#### `.gitignore`
```text
.env
credentials/
downloads/
state/
__pycache__/
*.pyc
```

#### `.env.example`
```text
# OpenAI (used for PDF/image invoice extraction — see scripts/extract_invoice.py)
OPENAI_API_KEY=your-api-key-here

# Google auth is OAuth (credentials/oauth-client.json + credentials/token.json).
# Run: python scripts/authorize.py

# Drive folder to watch for new invoice files. Also doubles as the parent
# for this agent's own "Agent Data" subfolder, which holds its
# Invoice_Log and Vendor_Master Google Sheets. Created automatically on
# first run.
DRIVE_WATCH_FOLDER_ID=your-drive-folder-id

# Gmail search query used to find new invoice emails. Already-read mail is
# included on purpose (no is:unread) — how far back it looks is controlled by
# EMAIL_CHECK_START_DATE / the rolling checkpoint below, not read status.
# Matches on SUBJECT LINE only, case-insensitively, and deliberately has no
# has:attachment requirement — a real invoice can be typed or forwarded
# directly into the email body with no file attached at all.
GMAIL_QUERY=subject:(invoice OR invoices)

# The very first run has no checkpoint yet, so it needs a starting point:
# only emails on/after this date are considered. Format YYYY-MM-DD. Leave
# blank to default to today, so a first run doesn't crawl your entire
# mailbox history. Every run after the first ignores this and resumes from
# exactly where the previous run left off instead.
EMAIL_CHECK_START_DATE=

# Where "needs review" alerts get sent
NOTIFY_EMAIL=you@company.com

# How many invoices to extract concurrently in one run (each is an independent
# OpenAI call). Raise this for larger batches, but stay under your OpenAI
# rate limit — check your tier's requests-per-minute before increasing.
MAX_CONCURRENT_EXTRACTIONS=4
```

#### `scripts/config.py`
```python
"""Central place for environment configuration. Everything reads from .env."""
import os
from datetime import date

from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Controlled vocabulary invoices get classified into. Customize this list for
# your business during setup (see the interview in SETUP_PROMPT.md) — a
# category outside this list can't be trusted downstream, so extraction must
# pick exactly one of these values or return null rather than forcing a bad
# fit (see EXTRACTION_SCHEMA's enum in extract_invoice.py).
AP_CATEGORIES = [
    "Raw Materials",
    "Packaging",
    "MRO Supplies",
    "Professional Services",
    "Software & Subscriptions",
    "Logistics & Freight",
    "Facilities & Utilities",
]

# The watched Drive folder. Doubles as the parent for this agent's own
# "Agent Data" subfolder (Invoice_Log/Vendor_Master sheets) — see
# scripts/sheets_client.py — as well as the tree intake_drive.py walks for
# new invoice files.
DRIVE_WATCH_FOLDER_ID = os.environ.get("DRIVE_WATCH_FOLDER_ID")
# No is:unread here on purpose — an invoice that arrived before this agent
# existed, or that a person already opened, is still an invoice. Already-read
# mail is included; scripts/intake_gmail.py bounds *how far back* it looks
# using EMAIL_CHECK_START_DATE / the rolling checkpoint below instead.
GMAIL_QUERY = os.environ.get("GMAIL_QUERY", "subject:(invoice OR invoices)")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL")

# The very first time intake_gmail.py runs (before it has a saved checkpoint
# in state/gmail_last_checked.json), it starts searching from this date.
# Every run after that starts from where the previous run left off instead —
# see EMAIL_CHECK_START_DATE's use in intake_gmail.py. Defaults to today if
# unset, so an agent that's never run before doesn't crawl your entire
# mailbox history on its first pass.
_email_check_start_date_raw = os.environ.get("EMAIL_CHECK_START_DATE")
if _email_check_start_date_raw:
    try:
        date.fromisoformat(_email_check_start_date_raw)
    except ValueError:
        raise RuntimeError(
            f"EMAIL_CHECK_START_DATE must be in YYYY-MM-DD format, got: {_email_check_start_date_raw!r}"
        )
    EMAIL_CHECK_START_DATE = _email_check_start_date_raw
else:
    EMAIL_CHECK_START_DATE = date.today().isoformat()

# How many invoices main.py extracts concurrently (each is an independent,
# stateless OpenAI call). Bound this to stay under your OpenAI rate limit
# rather than raising it without checking your tier's requests-per-minute.
MAX_CONCURRENT_EXTRACTIONS = int(os.environ.get("MAX_CONCURRENT_EXTRACTIONS", "4"))


def require(value, name):
    if not value:
        raise RuntimeError(f"Missing required config: {name}. Check your .env file.")
    return value
```

#### `scripts/auth.py`
```python
"""Shared OAuth credentials for Gmail, Drive, and Sheets.

Uses a single user-consented OAuth client rather than a service account:
a consumer @gmail.com inbox cannot be read by a service account at all,
and acting as the user removes the need to share folders/sheets with a
separate robot address.

Run scripts/authorize.py once to perform the browser consent.
"""
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

CREDENTIALS_DIR = Path(__file__).resolve().parent.parent / "credentials"
CLIENT_SECRETS_PATH = CREDENTIALS_DIR / "oauth-client.json"
TOKEN_PATH = CREDENTIALS_DIR / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    # Full (not .readonly/.file) Drive scope: this agent both reads the
    # watched Invoice_Automation folder tree AND creates/writes its own
    # "Agent Data" subfolder + sheets inside a folder it didn't create —
    # drive.file's "only files this app created or the user opened with
    # it" restriction doesn't cover that pre-existing parent folder.
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_credentials(interactive: bool = False) -> Credentials:
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
        return creds

    if not interactive:
        raise RuntimeError(
            f"No usable Google credentials at {TOKEN_PATH}. "
            "Run: python scripts/authorize.py"
        )

    if not CLIENT_SECRETS_PATH.exists():
        raise RuntimeError(f"Missing OAuth client file at {CLIENT_SECRETS_PATH}.")

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    return creds
```

#### `scripts/authorize.py`
```python
"""One-time Google consent, then prints the IDs needed for .env.

Run:  python scripts/authorize.py
Opens a browser once; stores a refresh token in credentials/token.json.
"""
from googleapiclient.discovery import build

from auth import get_credentials

FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def _find(drive, mime, name_contains=None):
    q = f"mimeType = '{mime}' and trashed = false"
    if name_contains:
        q += f" and name contains '{name_contains}'"
    out, token = [], None
    while True:
        r = drive.files().list(
            q=q, fields="nextPageToken, files(id, name, parents)",
            pageSize=100, pageToken=token,
        ).execute()
        out.extend(r.get("files", []))
        token = r.get("nextPageToken")
        if not token:
            return out


def main():
    creds = get_credentials(interactive=True)
    print("\nAuthorized. Token saved to credentials/token.json\n")

    drive = build("drive", "v3", credentials=creds)

    print("=" * 60)
    print("DRIVE FOLDERS")
    print("=" * 60)
    for f in _find(drive, FOLDER_MIME):
        print(f"  {f['name']:<35} {f['id']}")

    print()
    print("=" * 60)
    print("SPREADSHEETS")
    print("=" * 60)
    for f in _find(drive, SHEET_MIME):
        print(f"  {f['name']:<35} {f['id']}")
    print()


if __name__ == "__main__":
    main()
```

#### `scripts/dedup.py`
```python
"""
Shared content-hash ledger so the same file bytes are only ever processed
once, no matter which intake source they came from, what they're named, or
who put them there. This is what catches the cases the per-source ledgers
in intake_gmail.py / intake_drive.py can't:

- the same PDF sent as an attachment in two different emails
- the same invoice emailed AND separately dropped in the watched Drive folder
- two different people saving the same file into the Drive folder under
  different filenames

The per-source ledgers only stop the exact same Gmail message or Drive file
ID from being reprocessed — a different, narrower guarantee that doesn't
help when the duplicate arrives under a different ID.

This is a purely mechanical check (identical bytes). It intentionally does
NOT try to catch near-duplicates (a rescanned copy, a re-exported PDF of the
same invoice) — that's what the `duplicate_invoice` check in
validate_invoice.py is for, using the extracted vendor/invoice_number/date
instead of raw bytes, and it flags for human review rather than skipping.
"""
import hashlib
import json
from pathlib import Path

LEDGER_PATH = Path(__file__).resolve().parent.parent / "state" / "processed_content_hashes.json"


def _load() -> set:
    if LEDGER_PATH.exists():
        return set(json.loads(LEDGER_PATH.read_text()))
    return set()


def _save(hashes: set) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(sorted(hashes)))


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def already_seen(data: bytes) -> bool:
    return hash_bytes(data) in _load()


def mark_seen(data: bytes) -> None:
    hashes = _load()
    hashes.add(hash_bytes(data))
    _save(hashes)
```

#### `scripts/extract_invoice.py`
```python
"""
Reads a PDF/image invoice/receipt, OR the plain-text body of an email, and
returns structured data using OpenAI. Works for formal vendor invoices and
plain retail receipts (Walmart, etc.) alike.
"""
import base64
import json
import mimetypes
from pathlib import Path

from openai import OpenAI

from config import OPENAI_API_KEY, AP_CATEGORIES, require

# Swap this for whichever current OpenAI vision/PDF-capable model fits your
# cost/accuracy needs. gpt-4o reads PDFs natively via the Responses API
# (each page is understood directly, no separate OCR/rasterize step needed).
MODEL = "gpt-4o"

_CATEGORY_LIST = "\n".join(f'  - "{c}"' for c in AP_CATEGORIES)

_FIELDS_DESCRIPTION = f"""- vendor: the vendor/merchant name
- invoice_number: use the receipt/transaction number if there's no formal invoice number; null if truly absent
- invoice_date: YYYY-MM-DD
- line_items: description, quantity, unit_price, amount for each line
- subtotal, tax, total: numbers
- po_number: null if not present
- po_line: a line number on the PO (e.g. "line 2" or "item 2") — only if the document itself references one; never guess
- category: EXACTLY one of the following, or null if none genuinely fits:
{_CATEGORY_LIST}
  Do not force a fit — e.g. a grocery or general retail receipt (food, household goods) fits none of these; return null rather than picking the closest-sounding one.
- currency: e.g. "USD\""""

EXTRACTION_PROMPT = f"""You are reading a vendor invoice or store receipt (it may be a PDF or a photo of a paper receipt, e.g. from Walmart or any other shop).

Extract these fields:

{_FIELDS_DESCRIPTION}

If a field genuinely isn't present on the document, use null (or 0 for money you can compute from other fields). Never invent a value that isn't visibly on the document. If line items aren't itemized (e.g. a simple receipt with just a total), return a single line item with a reasonable description and the total amount.
"""

# Used for email bodies matched by subject line alone (see intake_gmail.py's
# GMAIL_QUERY — it no longer requires has:attachment), so plenty of matches
# won't actually contain a real invoice: a reply discussing one, a marketing
# email that just uses the word, a forwarded thread with no amounts. The
# not_an_invoice escape hatch lets the model say so instead of hallucinating
# fields to fit the schema.
TEXT_EXTRACTION_PROMPT = f"""You are reading the plain-text body of an email. It may be a forwarded order confirmation, invoice, or receipt — possibly mixed in with quoted headers, signatures, or unrelated marketing boilerplate.

First decide: does this email body actually contain a real invoice, receipt, or order confirmation with concrete line items and a total amount — not just a mention of the word "invoice"? If it does NOT, set not_an_invoice to true and leave every other field null (or an empty list for line_items).

If it DOES contain a real invoice/receipt, set not_an_invoice to false and extract:

{_FIELDS_DESCRIPTION}

If a field genuinely isn't present, use null (or 0 for money you can compute from other fields). Never invent a value that isn't visibly in the text. If line items aren't itemized, return a single line item with a reasonable description and the total amount.
"""

_LINE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "quantity": {"type": "number"},
        "unit_price": {"type": "number"},
        "amount": {"type": "number"},
    },
    "required": ["description", "quantity", "unit_price", "amount"],
    "additionalProperties": False,
}

# Base field definitions, shared by both schemas below. The text-extraction
# path (with its not_an_invoice escape hatch) needs every one of these to
# also accept null, since OpenAI's strict Structured Outputs mode requires
# every property to be present in the response even when there's nothing to
# fill in (e.g. a "not an invoice" reply).
_INVOICE_FIELDS = {
    "vendor": {"type": "string"},
    "invoice_number": {"type": ["string", "null"]},
    "invoice_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
    "line_items": {"type": "array", "items": _LINE_ITEM_SCHEMA},
    "subtotal": {"type": "number"},
    "tax": {"type": "number"},
    "total": {"type": "number"},
    "po_number": {"type": ["string", "null"]},
    "po_line": {"type": ["integer", "null"]},
    "category": {"type": ["string", "null"], "enum": [*AP_CATEGORIES, None]},
    "currency": {"type": "string"},
}


def _nullable(field_schema: dict) -> dict:
    types = field_schema["type"]
    types = types if isinstance(types, list) else [types]
    if "null" not in types:
        types = [*types, "null"]
    return {**field_schema, "type": types}


EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": _INVOICE_FIELDS,
    "required": list(_INVOICE_FIELDS.keys()),
    "additionalProperties": False,
}

TEXT_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "not_an_invoice": {"type": "boolean"},
        **{name: _nullable(schema) for name, schema in _INVOICE_FIELDS.items()},
    },
    "required": ["not_an_invoice", *_INVOICE_FIELDS.keys()],
    "additionalProperties": False,
}


def _media_type(file_path: str) -> str:
    mime, _ = mimetypes.guess_type(file_path)
    if mime not in ("application/pdf", "image/jpeg", "image/png", "image/webp"):
        raise ValueError(f"Unsupported file type for {file_path}: {mime}")
    return mime


def _file_content_block(file_path: str) -> dict:
    media_type = _media_type(file_path)
    data = base64.standard_b64encode(Path(file_path).read_bytes()).decode("utf-8")
    if media_type == "application/pdf":
        return {
            "type": "input_file",
            "filename": Path(file_path).name,
            "file_data": f"data:{media_type};base64,{data}",
        }
    return {"type": "input_image", "image_url": f"data:{media_type};base64,{data}"}


def _run_extraction(
    content: list[dict], schema_name: str, schema: dict, source_name: str, allow_not_an_invoice: bool
) -> dict | None:
    """Shared OpenAI call + JSON parsing for both the file-based and
    text-based extraction paths below. Returns None if the model reports
    `not_an_invoice` (only possible via TEXT_EXTRACTION_SCHEMA — the file
    path's schema has no such field, so it should never see it)."""
    require(OPENAI_API_KEY, "OPENAI_API_KEY")
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.responses.create(
        model=MODEL,
        input=[{"role": "user", "content": content}],
        max_output_tokens=4096,  # headroom for long itemized receipts (e.g. 20+ line items)
        text={"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}},
    )

    try:
        record = json.loads(response.output_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"OpenAI did not return valid JSON for {source_name}: {response.output_text[:200]}") from e

    if allow_not_an_invoice and record.pop("not_an_invoice", False):
        return None

    record["source_file"] = source_name
    return record


def extract_invoice_data(file_path: str) -> dict:
    """Send a PDF or image to OpenAI and return the extracted fields as a dict."""
    content = [_file_content_block(file_path), {"type": "input_text", "text": EXTRACTION_PROMPT}]
    record = _run_extraction(
        content, "invoice_extraction", EXTRACTION_SCHEMA, Path(file_path).name, allow_not_an_invoice=False
    )
    if record is None:
        raise ValueError(f"Unexpected not_an_invoice response for a file extraction: {file_path}")
    return record


def extract_invoice_data_from_text(text: str, source_name: str) -> dict | None:
    """Send an email body's plain text to OpenAI and return the extracted
    fields, or None if the model determines the body doesn't actually
    contain a real invoice/receipt — expected and common now that Gmail
    intake matches on subject line alone (see intake_gmail.py), not on
    having a genuine invoice attached."""
    content = [
        {"type": "input_text", "text": f"EMAIL BODY:\n\n{text}"},
        {"type": "input_text", "text": TEXT_EXTRACTION_PROMPT},
    ]
    return _run_extraction(
        content, "email_invoice_extraction", TEXT_EXTRACTION_SCHEMA, source_name, allow_not_an_invoice=True
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python extract_invoice.py <path-to-invoice>")
        sys.exit(1)
    print(json.dumps(extract_invoice_data(sys.argv[1]), indent=2))
```

#### `scripts/validate_invoice.py`
```python
"""
Validation logic for extracted invoice records. Pure functions, no external
dependencies, so this is fully testable offline (see tests/test_validate_invoice.py).
"""
import difflib
import re

from config import AP_CATEGORIES

AMOUNT_TOLERANCE = 0.02
FUZZY_MATCH_CUTOFF = 0.8


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def resolve_vendor_id(vendor: str, master_vendors: list[dict]) -> str | None:
    """Matches the extracted vendor name against vendor_name + aliases in
    Vendor_Master, returning its vendor_id — the join key this pipeline
    (and any downstream AP/ERP system) keys on."""
    if not vendor:
        return None
    normalized = _normalize(vendor)
    candidates = {}  # normalized name/alias -> vendor_id
    for row in master_vendors:
        vendor_id = row.get("vendor_id")
        if not vendor_id:
            continue
        name = _normalize(row.get("vendor_name", ""))
        if name:
            candidates[name] = vendor_id
        for alias in (row.get("aliases") or "").split(","):
            alias = _normalize(alias)
            if alias:
                candidates[alias] = vendor_id

    if normalized in candidates:
        return candidates[normalized]
    match = difflib.get_close_matches(normalized, candidates.keys(), n=1, cutoff=FUZZY_MATCH_CUTOFF)
    return candidates[match[0]] if match else None


def _category_issue(category) -> str | None:
    """Category is a controlled vocabulary (AP_CATEGORIES in config.py) so
    downstream matching/reporting can key on it reliably — a value outside
    that list can't be trusted, and no category at all (routine for a
    retail receipt with no natural fit, e.g. groceries) means a human needs
    to assign one before the invoice moves forward."""
    if not category:
        return "category_unresolved: no category could be assigned"
    if category not in AP_CATEGORIES:
        return f"category_invalid: '{category}' is not one of {AP_CATEGORIES}"
    return None


def _math_checks_out(record: dict) -> bool:
    line_items = record.get("line_items") or []
    items_sum = sum(float(item.get("amount", 0) or 0) for item in line_items)
    subtotal = float(record.get("subtotal", 0) or 0)
    tax = float(record.get("tax", 0) or 0)
    total = float(record.get("total", 0) or 0)

    if line_items and abs(items_sum - subtotal) > AMOUNT_TOLERANCE:
        return False
    if abs((subtotal + tax) - total) > AMOUNT_TOLERANCE:
        return False
    return True


def _po_looks_valid(po_number) -> bool:
    if po_number in (None, ""):
        return True  # missing PO is fine, e.g. retail receipts with no purchase order at all
    return bool(re.match(r"^[A-Za-z0-9\-]+$", str(po_number)))


def _find_duplicate(record: dict, existing_rows: list[dict], vendor_id: str | None) -> dict | None:
    """Matches on vendor_id (or vendor name, if not yet resolved) +
    invoice_number when a number is present; falls back to vendor +
    invoice_date + total for receipts with no formal number."""
    invoice_number = _normalize(str(record.get("invoice_number") or ""))
    vendor_norm = _normalize(record.get("vendor", ""))
    if not vendor_id and not vendor_norm:
        return None

    for row in existing_rows:
        if vendor_id and row.get("vendor_id"):
            if row.get("vendor_id") != vendor_id:
                continue
        elif _normalize(row.get("vendor_name") or row.get("vendor") or "") != vendor_norm:
            continue

        if invoice_number:
            if _normalize(str(row.get("invoice_number") or "")) == invoice_number:
                return row
            continue
        same_date = str(row.get("invoice_date", "")) == str(record.get("invoice_date", ""))
        try:
            same_total = abs(float(row.get("total", 0) or 0) - float(record.get("total", 0) or 0)) <= AMOUNT_TOLERANCE
        except (TypeError, ValueError):
            same_total = False
        if same_date and same_total:
            return row
    return None


def validate_invoice(
    record: dict,
    master_vendors: list[dict],
    po_list: list[str] | None = None,
    existing_rows: list[dict] | None = None,
) -> dict:
    """Returns {"status": "verified" | "needs_review", "issues": [str, ...],
    "vendor_id": str | None}.

    `existing_rows` is optional so callers that don't have the Invoice Log
    loaded (e.g. the vendor-approval re-validation pass) don't need to fake it."""
    issues = []

    vendor_id = resolve_vendor_id(record.get("vendor", ""), master_vendors)
    if vendor_id is None:
        issues.append(f"vendor_not_found: '{record.get('vendor')}' is not in the master vendor list")

    category_issue = _category_issue(record.get("category"))
    if category_issue:
        issues.append(category_issue)

    if not _math_checks_out(record):
        issues.append("math_mismatch: line items / subtotal / tax / total don't reconcile")

    if not _po_looks_valid(record.get("po_number")):
        issues.append(f"po_malformed: '{record.get('po_number')}'")

    if po_list is not None and record.get("po_number") and record["po_number"] not in po_list:
        issues.append(f"po_not_found: '{record.get('po_number')}' is not an open PO")

    if existing_rows:
        dup = _find_duplicate(record, existing_rows, vendor_id)
        if dup is not None:
            issues.append(
                f"duplicate_invoice: matches an existing row for vendor '{record.get('vendor')}'"
                + (f", invoice_number '{record.get('invoice_number')}'" if record.get("invoice_number")
                   else " (same invoice_date and total, no invoice_number to key on)")
            )

    return {"status": "verified" if not issues else "needs_review", "issues": issues, "vendor_id": vendor_id}
```

#### `scripts/sheets_client.py`
```python
"""
Reads and writes this agent's OWN Invoice_Log and Vendor_Master Google
Sheets, kept in a dedicated "Agent Data" subfolder inside the watched
'Invoice_Automation' Drive folder (DRIVE_WATCH_FOLDER_ID). The column
layout is a generic AP-friendly schema (see references/sheet_schema.md) so
rows can be copied by hand into a downstream AP/ERP system if you have one
— but this agent only ever reads/writes its own two sheets, never an
external file.

This module must never open a spreadsheet other than the two it owns in
the Agent Data folder — see the design rule in references/sheet_schema.md.
Writing into someone else's shared file directly is exactly the failure
mode this separation avoids.

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

# A generic AP-friendly schema (references/sheet_schema.md documents what
# each column means) — the first several columns match common downstream
# AP/ERP import formats; everything after invoice_date is this agent's own
# addition.
INVOICE_LOG_COLUMNS = [
    "invoice_number", "vendor_id", "vendor_name", "po_number", "po_line",
    "category", "qty_invoiced", "unit_price", "total", "currency", "invoice_date",
    "subtotal", "tax", "date_received", "logged_at", "source", "file_name",
    "line_items", "review_status", "issue", "approve_vendor",
]

# A generic vendor master schema — country/tax/payment-terms/criticality
# fields common to AP onboarding, left blank by this agent (see add_vendor()).
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
    There's no pre-existing data here to be careful around."""
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
    This is the only place a multi-item receipt's itemization survives:
    qty_invoiced/unit_price collapse it to a single row."""
    if not line_items:
        return ""
    return "; ".join(
        f"{item.get('description', '') or '(no description)'} ({_format_amount(item.get('amount', 0))})"
        for item in line_items
    )


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
    criticality, bank details...) is left blank — the extraction model has
    no way to know these from an invoice. If your downstream AP/ERP system
    also gates on an Active-only status, a 'Pending' vendor stays inert
    there until a human finishes onboarding it directly.

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
    """Appends a row to Invoice_Log.

    Multi-item receipts (no formal qty/unit_price on the document) collapse
    to a single row: qty_invoiced=1, unit_price=subtotal. The full
    itemization is preserved in the line_items column.
    """
    values = {
        "invoice_number": record.get("invoice_number") or "",
        "vendor_id": record.get("vendor_id") or "",
        "vendor_name": record.get("vendor", ""),
        "po_number": record.get("po_number") or "",
        "po_line": record.get("po_line") or "",
        "category": record.get("category") or "",
        "qty_invoiced": record.get("qty_invoiced", 1),
        "unit_price": record.get("unit_price", record.get("subtotal", 0)),
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
```

#### `scripts/intake_drive.py`
```python
"""
Lists new files in a watched Drive folder tree and downloads them locally
for processing. "New" = not already present in a local processed-ids ledger.

The watched folder is walked RECURSIVELY: invoices land both directly in
'Attachments from Gmail' (archived there by intake_gmail.py itself — see
its module docstring) and inside per-month subfolders of 'Invoice Inbox',
so a one-level listing would miss most of them.
"""
import io
import json
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

import dedup
from auth import get_credentials
from config import DRIVE_WATCH_FOLDER_ID, require

FOLDER_MIME = "application/vnd.google-apps.folder"

# Only what extract_invoice.py can actually read. This also excludes native
# Google files: the Invoice Log and Vendor_Master sheets live inside the
# watched folder, and get_media 403s on Docs-editor files rather than
# returning bytes, which would abort the whole run.
SUPPORTED_MIMES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
}
LEDGER_PATH = Path(__file__).resolve().parent.parent / "state" / "processed_drive_ids.json"
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"


def _service():
    return build("drive", "v3", credentials=get_credentials())


def _load_ledger() -> set:
    if LEDGER_PATH.exists():
        return set(json.loads(LEDGER_PATH.read_text()))
    return set()


def _save_ledger(ids: set) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(sorted(ids)))


def _list_children(service, folder_id):
    """Yields every direct child (files and folders) of one folder."""
    page_token = None
    while True:
        # Drive's default page size is 100 and this call was previously
        # unpaginated — past ~100 files it would silently stop seeing
        # anything beyond the first page, forever, not just "next run".
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        yield from response.get("files", [])
        page_token = response.get("nextPageToken")
        if not page_token:
            return


def _walk(service, root_id):
    """Breadth-first walk of the folder tree, yielding (file, folder_path).

    Tracks visited folder ids because Drive allows a folder to sit under
    more than one parent, which would otherwise revisit a whole subtree.
    """
    queue = [(root_id, "")]
    seen_folders = {root_id}
    while queue:
        folder_id, prefix = queue.pop(0)
        for child in _list_children(service, folder_id):
            if child["mimeType"] == FOLDER_MIME:
                if child["id"] not in seen_folders:
                    seen_folders.add(child["id"])
                    queue.append((child["id"], f"{prefix}/{child['name']}".lstrip("/")))
            elif child["mimeType"] in SUPPORTED_MIMES:
                yield child, prefix


def fetch_new_files() -> list[str]:
    """Downloads any new files in the watched tree and returns local paths.

    Two layers keep this from double-processing the same invoice:
    1. `processed` (this file's ledger) skips a Drive file ID we've already
       handled in a previous run.
    2. `dedup.py` skips a file whose exact byte content has already been
       processed under ANY source — this is what catches the same PDF
       already archived by `intake_gmail.py` and also dropped by hand into
       a month folder.
    """
    require(DRIVE_WATCH_FOLDER_ID, "DRIVE_WATCH_FOLDER_ID")
    service = _service()
    processed = _load_ledger()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    new_paths = []
    for f, folder_path in _walk(service, DRIVE_WATCH_FOLDER_ID):
        if f["id"] in processed:
            continue

        # Drive ids prefix the local name: two different invoices both called
        # "invoice.pdf" in different month folders would otherwise overwrite
        # each other here before either got extracted.
        local_path = DOWNLOAD_DIR / f"{f['id']}__{f['name']}"
        request = service.files().get_media(fileId=f["id"])
        with io.FileIO(local_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

        data = local_path.read_bytes()
        if dedup.already_seen(data):
            local_path.unlink()  # exact same file already processed via this or another source
        else:
            dedup.mark_seen(data)
            new_paths.append(str(local_path))

        # Mark processed and save immediately, per file — not batched at the
        # end — so a crash partway through a run can't cause an
        # already-finished file to be replayed next time.
        processed.add(f["id"])
        _save_ledger(processed)

    return new_paths
```

#### `scripts/intake_gmail.py`
```python
"""
Finds candidate invoices in Gmail: downloads attachments locally for
extraction (and archives a copy into the watched Drive folder's
'Attachments from Gmail' subfolder), AND separately extracts each matched
message's plain-text body as its own candidate invoice — an invoice can
arrive typed or forwarded directly into an email with no attachment at all,
which is why GMAIL_QUERY matches on subject line alone (see
`_search_query()`) rather than requiring `has:attachment`.

NOTE: a plain service account cannot read a personal Gmail inbox on its own —
Gmail's API requires either (a) domain-wide delegation on a Google Workspace
account, or (b) a per-user OAuth flow (the user consents once, a refresh
token is stored). This project uses (b) — see auth.py / authorize.py.
"""
import base64
import io
import json
import re
import time
from pathlib import Path

from auth import get_credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

import dedup
from config import GMAIL_QUERY, EMAIL_CHECK_START_DATE, DRIVE_WATCH_FOLDER_ID, require

# Read-only is enough for Gmail itself now that nothing here ever modifies a
# message. If you ever add write behavior against Gmail, this needs to go
# back to gmail.modify. Archiving to Drive uses the separate `drive` scope
# in auth.py.
LEDGER_PATH = Path(__file__).resolve().parent.parent / "state" / "processed_gmail_ids.json"
LAST_CHECKED_PATH = Path(__file__).resolve().parent.parent / "state" / "gmail_last_checked.json"
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "downloads"

FOLDER_MIME = "application/vnd.google-apps.folder"
GMAIL_ATTACHMENTS_FOLDER_NAME = "Attachments from Gmail"

# Memoized within one process run, same "load once" pattern as
# sheets_client.py's _ids_cache.
_archive_folder_id_cache: str | None = None


def _service():
    return build("gmail", "v1", credentials=get_credentials())


def _drive_service():
    return build("drive", "v3", credentials=get_credentials())


def _get_archive_folder_id(drive) -> str:
    """Finds the existing 'Attachments from Gmail' subfolder inside the
    watched Invoice_Automation folder, creating it if it's somehow missing."""
    global _archive_folder_id_cache
    if _archive_folder_id_cache:
        return _archive_folder_id_cache
    require(DRIVE_WATCH_FOLDER_ID, "DRIVE_WATCH_FOLDER_ID")

    q = (
        f"'{DRIVE_WATCH_FOLDER_ID}' in parents and name = '{GMAIL_ATTACHMENTS_FOLDER_NAME}' "
        f"and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    resp = drive.files().list(q=q, fields="files(id)", pageSize=10).execute()
    files = resp.get("files", [])
    if files:
        folder_id = files[0]["id"]
    else:
        folder = drive.files().create(
            body={
                "name": GMAIL_ATTACHMENTS_FOLDER_NAME,
                "mimeType": FOLDER_MIME,
                "parents": [DRIVE_WATCH_FOLDER_ID],
            },
            fields="id",
        ).execute()
        folder_id = folder["id"]
    _archive_folder_id_cache = folder_id
    return folder_id


def _archive_to_drive(drive, folder_id: str, filename: str, mime_type: str, data: bytes) -> None:
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type or "application/octet-stream", resumable=False)
    drive.files().create(body={"name": filename, "parents": [folder_id]}, media_body=media, fields="id").execute()


def _walk_body_parts(part: dict) -> tuple[str, str]:
    """Recursively finds a message's text/plain and text/html body parts —
    unlike the shallow top-level scan used for attachments, this has to
    recurse: the body text usually sits one or more levels down inside a
    multipart/alternative part, not as a direct child of the payload."""
    mime = part.get("mimeType", "")
    body = part.get("body", {})
    plain = html = ""
    if mime == "text/plain" and "data" in body:
        plain = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
    elif mime == "text/html" and "data" in body:
        html = base64.urlsafe_b64decode(body["data"]).decode("utf-8", errors="replace")
    for sub in part.get("parts", []) or []:
        sub_plain, sub_html = _walk_body_parts(sub)
        plain = plain or sub_plain
        html = html or sub_html
    return plain, html


def _extract_body_text(payload: dict) -> str:
    """Best available plain-text rendering of a message body: prefers
    text/plain, falls back to a crude tag-strip of text/html if that's all
    the message has (some emails, especially marketing/transactional ones,
    have no text/plain alternative at all)."""
    plain, html = _walk_body_parts(payload)
    if plain.strip():
        return plain
    if html.strip():
        return re.sub(r"<[^>]+>", " ", html)
    return ""


def _load_ledger() -> set:
    if LEDGER_PATH.exists():
        return set(json.loads(LEDGER_PATH.read_text()))
    return set()


def _save_ledger(ids: set) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.write_text(json.dumps(sorted(ids)))


def _load_last_checked() -> int | None:
    if LAST_CHECKED_PATH.exists():
        return json.loads(LAST_CHECKED_PATH.read_text())
    return None


def _save_last_checked(unix_seconds: int) -> None:
    LAST_CHECKED_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_CHECKED_PATH.write_text(json.dumps(unix_seconds))


def _search_query() -> str:
    """Builds the query's `after:` bound.

    First-ever run (no checkpoint yet): starts from EMAIL_CHECK_START_DATE
    (.env; defaults to today), at whole-day precision — that's all Gmail's
    date-only after: syntax supports.

    Every run after that: starts from the unix-timestamp checkpoint saved by
    the previous run, which gives second-level precision — e.g. if the last
    run started checking at 3pm, this run's query begins `after:` that exact
    moment instead of re-scanning from the original start date every time.
    """
    last_checked = _load_last_checked()
    if last_checked is not None:
        after_clause = f"after:{last_checked}"
    else:
        after_clause = f"after:{EMAIL_CHECK_START_DATE.replace('-', '/')}"
    return f"{GMAIL_QUERY} {after_clause}"


def fetch_new_invoice_sources() -> list[dict]:
    """Returns candidate invoice jobs from new matching messages (read or
    unread), each one a dict:

      {"kind": "file", "path": "<local path>"}
      {"kind": "text", "text": "<body text>", "label": "<for logs/source_file>"}

    Every new attachment is downloaded locally AND archived into the watched
    Drive folder's 'Attachments from Gmail' subfolder. Separately, every new
    matching message's body text is extracted as its own candidate, since
    GMAIL_QUERY matches on subject line alone — many real invoices arrive
    typed or forwarded directly into the email, not as an attachment.
    extract_invoice.py's `not_an_invoice` escape hatch is what filters out
    the ones that turn out to just mention the word.

    Two layers keep this from double-processing the same content:
    1. `processed` (this file's ledger) skips a Gmail message ID we've
       already handled in a previous run — this gates the whole message,
       attachments and body alike.
    2. `dedup.py` skips any attachment or body text whose exact bytes have
       already been processed under ANY source — this is what catches the
       same PDF being sent in two emails, or emailed and also dropped in
       the watched Drive folder. A byte-identical file already archived
       this way on a previous run is also what `intake_drive.py` will skip
       via this same layer if it re-walks the folder and sees it there.

    Which messages are even considered is narrowed by `_search_query()`'s
    `after:` bound (see there) — that's a search-window optimization, not a
    duplicate guard by itself: if the window ever overlaps a previous run
    (e.g. this run crashes before saving a new checkpoint), the id ledger
    above is what actually keeps an already-handled message from being
    reprocessed, not the date bound.
    """
    run_started_at = int(time.time())
    service = _service()
    drive = _drive_service()
    archive_folder_id = _get_archive_folder_id(drive)
    processed = _load_ledger()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    query = _search_query()
    messages = service.users().messages().list(userId="me", q=query).execute().get("messages", [])
    jobs = []

    for msg_meta in messages:
        if msg_meta["id"] in processed:
            continue
        msg = service.users().messages().get(userId="me", id=msg_meta["id"]).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("Subject") or "(no subject)"

        for part in msg.get("payload", {}).get("parts", []):
            filename = part.get("filename")
            if not filename or "attachmentId" not in part.get("body", {}):
                continue
            att = service.users().messages().attachments().get(
                userId="me", messageId=msg_meta["id"], id=part["body"]["attachmentId"]
            ).execute()
            data = base64.urlsafe_b64decode(att["data"])
            if dedup.already_seen(data):
                continue  # exact same file already processed via this or another source
            local_path = DOWNLOAD_DIR / filename
            local_path.write_bytes(data)
            dedup.mark_seen(data)
            jobs.append({"kind": "file", "path": str(local_path)})
            _archive_to_drive(drive, archive_folder_id, filename, part.get("mimeType"), data)

        body_text = _extract_body_text(msg.get("payload", {}))
        if body_text.strip():
            body_bytes = body_text.encode("utf-8")
            if not dedup.already_seen(body_bytes):
                dedup.mark_seen(body_bytes)
                jobs.append({
                    "kind": "text",
                    "text": body_text,
                    "label": f"email: {subject!r} (msg {msg_meta['id']})",
                })

        # Mark processed and save immediately, per message — not batched at
        # the end — so a crash partway through a run can't cause an
        # already-finished message to be replayed next time.
        processed.add(msg_meta["id"])
        _save_ledger(processed)

    # Only advance the checkpoint once every matched message above has been
    # handled without an exception escaping the loop. If this run crashes
    # partway through, next run's window should still cover the unfinished
    # messages rather than skip past them — already-finished ones just get
    # skipped instantly via the id ledger instead, which costs a slightly
    # wider search, never a missed invoice.
    _save_last_checked(run_started_at)

    return jobs
```

#### `scripts/notify.py`
```python
"""
Sends a one-way email alert when an invoice needs human review. The human
resolves it by editing the Invoice Log sheet directly (e.g. setting
approve_vendor = TRUE), not by replying to this email.
"""
import base64
from email.mime.text import MIMEText

from auth import get_credentials
from googleapiclient.discovery import build

from config import NOTIFY_EMAIL, require
from sheets_client import get_invoice_log_url


def send_review_email(record: dict, issues: list[str]) -> None:
    require(NOTIFY_EMAIL, "NOTIFY_EMAIL")

    subject = f"Invoice needs review: {record.get('vendor', 'unknown vendor')}"
    body = (
        f"Invoice {record.get('invoice_number', '(no number)')} from "
        f"{record.get('vendor', 'unknown vendor')} needs a decision:\n\n"
        + "\n".join(f"- {issue}" for issue in issues)
        + f"\n\nOpen the log to review and resolve: {get_invoice_log_url()}\n"
        + "To approve a new vendor, set approve_vendor = TRUE on that row and re-run the agent."
    )

    message = MIMEText(body)
    message["to"] = NOTIFY_EMAIL
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service = build("gmail", "v1", credentials=get_credentials())
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
```

#### `scripts/main.py`
```python
"""
Orchestrates the full pipeline: intake -> extract -> log -> validate -> notify,
plus a pass that applies any human-approved vendor additions.

Run with: python scripts/main.py
"""
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import MAX_CONCURRENT_EXTRACTIONS
from extract_invoice import extract_invoice_data, extract_invoice_data_from_text
from validate_invoice import validate_invoice
from sheets_client import (
    get_master_vendors, get_invoice_log_rows, append_invoice_row, add_vendor,
    get_rows_pending_approval, update_row_status, update_row_vendor_id,
)
from notify import send_review_email
import intake_drive
import intake_gmail


def process_new_invoices():
    master_vendors = get_master_vendors()
    # Loaded once and appended to in-memory as we go, so two copies of the
    # same invoice arriving from different sources in the same run (e.g.
    # emailed AND dropped in the watched Drive folder) still catch each other,
    # not just invoices that were already in the sheet before this run.
    existing_rows = get_invoice_log_rows()

    # Each job is (source_name, kind, payload, label): kind "file" means
    # payload is a local path (extract_invoice_data); kind "text" means
    # payload is an email body string (extract_invoice_data_from_text).
    # label is what gets logged/printed — the filename for "file" jobs, the
    # subject/message-id description intake_gmail.py built for "text" jobs.
    jobs = [("drive", "file", file_path, file_path) for file_path in intake_drive.fetch_new_files()]
    for source in intake_gmail.fetch_new_invoice_sources():
        if source["kind"] == "file":
            jobs.append(("email", "file", source["path"], source["path"]))
        else:
            jobs.append(("email", "text", source["text"], source["label"]))
    if not jobs:
        return

    # Extraction is the slow, token-spending step (one OpenAI call per file),
    # and each call is fully independent — no shared conversation, no state
    # carried between invoices — so token cost per invoice stays flat
    # regardless of batch size. That independence is exactly what makes it
    # safe to fan these calls out to a bounded pool of concurrent workers:
    # wall-clock time on a large batch drops from O(n) sequential API round
    # trips to roughly O(n / MAX_CONCURRENT_EXTRACTIONS), which is what
    # actually matters as invoice volume grows.
    #
    # Everything after extraction (validate, append, notify) stays
    # single-threaded on the main thread: it's cheap local logic, not an API
    # call, and the duplicate check depends on existing_rows being updated
    # one invoice at a time — parallelizing it would just add races for no
    # benefit.
    def _extract(kind, payload, label):
        if kind == "file":
            return extract_invoice_data(payload)
        return extract_invoice_data_from_text(payload, label)

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_EXTRACTIONS) as pool:
        futures = {
            pool.submit(_extract, kind, payload, label): (source_name, label)
            for source_name, kind, payload, label in jobs
        }
        for future in as_completed(futures):
            source_name, label = futures[future]
            try:
                record = future.result()
                if record is None:
                    # Only possible for "text" jobs: OpenAI decided the email
                    # body doesn't actually contain a real invoice.
                    print(f"[skipped] {label} (not an invoice)")
                    continue
                result = validate_invoice(record, master_vendors, existing_rows=existing_rows)
                record["vendor_id"] = result["vendor_id"]
                append_invoice_row(record, source_name, result["status"], result["issues"])
                existing_rows.append(record)
                if result["status"] == "needs_review":
                    send_review_email(record, result["issues"])
                print(f"[{result['status']}] {label}")
            except Exception:
                print(f"[error] {label}")
                traceback.print_exc()


def apply_approved_vendors():
    """Second pass: pick up any rows a human marked approve_vendor = TRUE."""
    pending = get_rows_pending_approval()
    if not pending:
        return

    # Loaded once, then appended to locally as vendors are added — same
    # pattern as existing_rows above. Re-fetching the whole vendor sheet
    # inside the loop would mean one full-sheet read per pending row, which
    # gets worse the more approvals land in a single run.
    master_vendors = get_master_vendors()

    for row in pending:
        vendor_name = row.get("vendor_name", "")
        if not vendor_name:
            continue
        vendor_id = add_vendor(vendor_name)
        master_vendors.append({"vendor_id": vendor_id, "vendor_name": vendor_name, "aliases": ""})
        record = {
            "vendor": vendor_name,
            "category": row.get("category"),
            "line_items": [],
            "subtotal": row.get("subtotal", 0),
            "tax": row.get("tax", 0),
            "total": row.get("total", 0),
            "po_number": row.get("po_number"),
        }
        result = validate_invoice(record, master_vendors)
        update_row_status(row["_row_number"], result["status"], result["issues"])
        if result["vendor_id"]:
            update_row_vendor_id(row["_row_number"], result["vendor_id"])
        print(
            f"[approved] {vendor_name} added to master list as {vendor_id} (status: Pending — "
            f"needs manual onboarding), row re-validated as {result['status']}"
        )


if __name__ == "__main__":
    process_new_invoices()
    apply_approved_vendors()
```

#### `tests/test_validate_invoice.py`
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from validate_invoice import validate_invoice

MASTER_VENDORS = [
    {"vendor_id": "V-1001", "vendor_name": "Walmart", "aliases": "WALMART #4521, Walmart Inc"},
    {"vendor_id": "V-1002", "vendor_name": "Acme Supplies", "aliases": ""},
]

CATEGORY = "Facilities & Utilities"  # any valid AP_CATEGORIES value


def test_known_vendor_clean_invoice_passes():
    record = {
        "vendor": "WALMART #4521",
        "category": CATEGORY,
        "line_items": [{"amount": 45.00}],
        "subtotal": 45.00,
        "tax": 3.60,
        "total": 48.60,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "verified", result["issues"]
    assert result["vendor_id"] == "V-1001"


def test_unknown_vendor_flags_for_review():
    record = {
        "vendor": "XYZ Corp",
        "category": CATEGORY,
        "line_items": [{"amount": 100.0}],
        "subtotal": 100.0,
        "tax": 0,
        "total": 100.0,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "needs_review"
    assert result["vendor_id"] is None
    assert any("vendor_not_found" in issue for issue in result["issues"])


def test_missing_category_flags_for_review():
    """Routine for a retail receipt with no natural fit (e.g. groceries) —
    it should be held for a human to assign a category, not guessed at."""
    record = {
        "vendor": "Walmart",
        "category": None,
        "line_items": [{"amount": 15.0}],
        "subtotal": 15.0,
        "tax": 1.2,
        "total": 16.2,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "needs_review"
    assert any("category_unresolved" in issue for issue in result["issues"])


def test_invalid_category_flags_for_review():
    record = {
        "vendor": "Walmart",
        "category": "Groceries",  # not one of AP_CATEGORIES
        "line_items": [{"amount": 15.0}],
        "subtotal": 15.0,
        "tax": 1.2,
        "total": 16.2,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "needs_review"
    assert any("category_invalid" in issue for issue in result["issues"])


def test_math_mismatch_flags_for_review():
    record = {
        "vendor": "Acme Supplies",
        "category": CATEGORY,
        "line_items": [{"amount": 50.0}],
        "subtotal": 50.0,
        "tax": 4.0,
        "total": 60.0,  # wrong, should be 54.0
        "po_number": "PO-1001",
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "needs_review"
    assert any("math_mismatch" in issue for issue in result["issues"])


def test_fuzzy_vendor_match_catches_typo():
    record = {
        "vendor": "Walmrt",  # typo, not an exact alias
        "category": CATEGORY,
        "line_items": [{"amount": 10.0}],
        "subtotal": 10.0,
        "tax": 0.8,
        "total": 10.8,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "verified", result["issues"]
    assert result["vendor_id"] == "V-1001"


def test_malformed_po_flags_for_review():
    record = {
        "vendor": "Acme Supplies",
        "category": CATEGORY,
        "line_items": [{"amount": 20.0}],
        "subtotal": 20.0,
        "tax": 1.6,
        "total": 21.6,
        "po_number": "!!not a po??",
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "needs_review"
    assert any("po_malformed" in issue for issue in result["issues"])


def test_duplicate_invoice_number_flags_for_review():
    existing_rows = [
        {"vendor_id": "V-1002", "vendor_name": "Acme Supplies", "invoice_number": "INV-500",
         "invoice_date": "2026-08-01", "total": 54.0},
    ]
    record = {
        "vendor": "Acme Supplies",  # same vendor, same invoice_number, different source
        "category": CATEGORY,
        "invoice_number": "INV-500",
        "invoice_date": "2026-08-01",
        "line_items": [{"amount": 50.0}],
        "subtotal": 50.0,
        "tax": 4.0,
        "total": 54.0,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS, existing_rows=existing_rows)
    assert result["status"] == "needs_review"
    assert any("duplicate_invoice" in issue for issue in result["issues"])


def test_duplicate_without_invoice_number_matches_on_date_and_total():
    existing_rows = [
        {"vendor_id": "V-1001", "vendor_name": "Walmart", "invoice_number": "",
         "invoice_date": "2026-08-01", "total": 16.2},
    ]
    record = {
        "vendor": "Walmart",
        "category": CATEGORY,
        "invoice_number": "",  # receipt with no number, e.g. retail till slip
        "invoice_date": "2026-08-01",
        "line_items": [{"amount": 15.0}],
        "subtotal": 15.0,
        "tax": 1.2,
        "total": 16.2,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS, existing_rows=existing_rows)
    assert result["status"] == "needs_review"
    assert any("duplicate_invoice" in issue for issue in result["issues"])


def test_no_existing_rows_skips_duplicate_check():
    record = {
        "vendor": "Walmart",
        "category": CATEGORY,
        "line_items": [{"amount": 15.0}],
        "subtotal": 15.0,
        "tax": 1.2,
        "total": 16.2,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)  # no existing_rows passed at all
    assert result["status"] == "verified", result["issues"]


def test_missing_po_is_not_a_failure():
    record = {
        "vendor": "Walmart",
        "category": CATEGORY,
        "line_items": [{"amount": 15.0}],
        "subtotal": 15.0,
        "tax": 1.2,
        "total": 16.2,
        "po_number": None,
    }
    result = validate_invoice(record, MASTER_VENDORS)
    assert result["status"] == "verified", result["issues"]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"\nAll {len(tests)} tests passed.")
```

#### `references/sheet_schema.md`
```markdown
# File schema

This agent keeps its **own** `Vendor_Master` and `Invoice_Log` Google Sheets
in a dedicated **"Agent Data"** subfolder inside the watched
`Invoice_Automation` Drive folder (`DRIVE_WATCH_FOLDER_ID` in `.env`). The
column layout is a generic AP-friendly schema so rows can be copied by hand
into a downstream AP/ERP system if you use one, but this stays a **separate
copy** — never the same physical file as anything else.

`scripts/sheets_client.py` is the only module that touches these sheets,
always by column **name** against the sheet's actual header row. Both
sheets are created automatically (empty, with just the header row) the
first time the agent runs — see `_get_or_create_sheet()`.

`intake_drive.py` walks `DRIVE_WATCH_FOLDER_ID` recursively looking for
invoice files, but only downloads MIME types it can actually extract from
(`SUPPORTED_MIMES`) — native Google Sheets are excluded, so these two
sheets living inside that same folder tree never get mistaken for an
invoice to process.

## Why these are separate sheets, not shared

**This agent must never open, read, or write any spreadsheet other than its
own two, in its own "Agent Data" folder.** If you're ever asked to make it
write into some other system's file directly instead, push back — routing
a bad extraction into someone else's live data is the exact failure mode
this separation exists to prevent. Getting invoices into a downstream
system is a human copy-paste step (or a separate, deliberate sync tool, if
you build one) — not something this agent does automatically.

## Invoice_Log

One row per invoice. The first several columns use field names common to
downstream AP/ERP import formats, so a row can usually be copied across
with little to no reshaping. Everything after `invoice_date` is this
agent's own addition.

| Column | Type | Notes |
|---|---|---|
| invoice_number | text | receipt/transaction number if there's no formal invoice number |
| vendor_id | text | resolved against Vendor_Master — `V-XXXX`. Blank when `vendor_not_found` |
| vendor_name | text | as extracted |
| po_number | text | blank if none |
| po_line | number | blank unless the document itself references a specific PO line |
| category | text | must be exactly one of `AP_CATEGORIES` (`config.py`), or blank if nothing genuinely fits — e.g. a grocery/retail receipt. Blank flags `category_unresolved` |
| qty_invoiced | number | `1` for a collapsed multi-item receipt — see `line_items` below |
| unit_price | number | `subtotal` for a collapsed receipt |
| total | number | |
| currency | text | e.g. `USD`, `CAD` |
| invoice_date | date | |
| subtotal | number | |
| tax | number | |
| date_received | date | when the file was picked up |
| logged_at | datetime | set once, at append time |
| source | text | `drive` or `email` |
| file_name | text | original file name |
| line_items | text | plain text, not JSON: `description ($amount); description ($amount)`. The only place a multi-item receipt's itemization survives — `qty_invoiced`/`unit_price` collapse it to one row. Built by `sheets_client.py::format_line_items()` |
| review_status | text | `verified` or `needs_review` |
| issue | text | blank, or a `;`-separated list of which checks failed |
| approve_vendor | boolean | human sets to `TRUE` to approve adding a new vendor found on this row |

## Vendor_Master

| Column | Type | Notes |
|---|---|---|
| vendor_id | text | join key — `V-XXXX`, next-available assigned by `sheets_client.py::add_vendor()` |
| vendor_name | text | canonical name |
| aliases | text | comma-separated, e.g. `WALMART #4521, Walmart Inc` — fuzzy-matched against the extracted vendor name |
| category | text | vendor's line of business (one of `AP_CATEGORIES`) |
| country | text | |
| tax_id_type, tax_id | text | e.g. W-9 vs W-8 |
| default_currency | text | |
| payment_terms | text | e.g. `"2/10 Net 30"` |
| criticality | text | `Critical`/`Standard` |
| status | text | `Active`/`Dormant`/`Pending` |
| bank_details_last_changed, bank_details_last_verified | date | |
| date_onboarded, onboarded_by | date, text | |

This sheet is only ever written to after a human sets `approve_vendor = TRUE`
on a flagged row — the agent never adds a vendor on its own. See the design
rule in `SKILL.md`.

**What `add_vendor()` actually writes**: only `vendor_id`, `vendor_name`,
`aliases`, `status="Pending"`, `date_onboarded`, `onboarded_by`. Every other
column is left blank — the extraction model has no way to know these from
an invoice. `status="Pending"` matters if a row is ever copied into a
downstream system that also gates on status: it keeps a `Pending` vendor
from flowing into payment until a human completes the row.

## Both sheets are created empty on first use

`sheets_client.py::_get_or_create_sheet()` creates each sheet (and the
"Agent Data" folder itself) with just the header row the first time it's
needed, if it doesn't already exist.
```

#### `references/validation_rules.md`
```markdown
# Validation rules

Full detail on every check `validate_invoice.py::validate_invoice()` runs.
All checks run independently — a row can fail more than one, and every
failure is recorded, not just the first. Result is
`{"status": "verified" | "needs_review", "issues": [...], "vendor_id": ...}`.

| # | Check | Condition | Issue code |
|---|---|---|---|
| 1 | **Vendor match** | Normalize (lowercase, strip to letters/digits) extracted vendor name; compare against `vendor_name` + every `aliases` entry in Vendor_Master. Exact match, or fuzzy match via `difflib.get_close_matches` at cutoff **0.8**. On match, resolves and returns the row's `vendor_id`. | `vendor_not_found` |
| 2 | **Category check** | `category` must be exactly one of `AP_CATEGORIES` (`config.py`). `null`/blank is common and expected for a document with no natural fit (e.g. a grocery receipt) — it's flagged, not guessed. | `category_unresolved` (blank) / `category_invalid` (present but not in the list) |
| 3 | **Math check** | `sum(line_items[].amount)` must equal `subtotal` within **$0.02** (skipped if no line items). `subtotal + tax` must equal `total` within **$0.02**. | `math_mismatch` |
| 4 | **PO check** | If `po_number` present, must match `^[A-Za-z0-9\-]+$` (letters/digits/hyphens). **Missing PO is not a failure** — plenty of real invoices/receipts legitimately have none. Optional `po_list` param (not wired up by default) would additionally check membership in an open-PO list. | `po_malformed` / `po_not_found` |
| 5 | **Duplicate check** | If `invoice_number` present: match on `vendor_id` (or normalized `vendor_name` if not yet resolved) + normalized `invoice_number` against every row already in the invoice log (including rows added earlier in the same run). If no `invoice_number`: fall back to vendor + `invoice_date` + `total` (within the $0.02 tolerance). A match **flags but does not skip** — two different invoices can coincidentally share a number, so a human decides. | `duplicate_invoice` |

**Tunable constants** (top of `validate_invoice.py`):
- `AMOUNT_TOLERANCE = 0.02`
- `FUZZY_MATCH_CUTOFF = 0.8` — loosen if legit vendors get flagged too often; tighten if unrelated vendors match each other

## Debugging a flagged row

Check the `issue` column on the row first — it names exactly which check(s) failed:

- `vendor_not_found` — check `Vendor_Master`; the extracted string must match a `vendor_name`/`aliases` entry within the 0.8 fuzzy cutoff. A real vendor with wording too different from what's stored (e.g. "Costco Wholesale" vs. stored "COSTCO") will flag even though it isn't really wrong — usually fixed by adding an alias.
- `category_unresolved` / `category_invalid` — no category could be assigned, or it isn't one of `AP_CATEGORIES`. Often correct, not a bug.
- `math_mismatch` — line items / subtotal / tax / total don't reconcile within $0.02.
- `po_malformed` — PO text doesn't look like a real identifier.
- `duplicate_invoice` — matches vendor+invoice_number, or vendor+date+total, of an existing row.
```

#### `README.md`
```markdown
# Invoice processing agent

An agent that watches for incoming invoices (Gmail attachments/body text, or
a Google Drive folder), extracts structured data using OpenAI, logs
everything to the `Invoice_Log` Google Sheet, validates it against the
`Vendor_Master` Google Sheet, and asks for human confirmation before
touching reference data.

## Pipeline

1. **Intake** — new files from a watched Gmail label/query or Drive folder; exact-duplicate bytes are skipped here regardless of which source or filename they arrive under (see `scripts/dedup.py`). Gmail attachments are also archived directly into the watched Drive folder's `Attachments from Gmail` subfolder.
2. **Extract** — OpenAI reads the PDF/image (or email body text) and returns structured JSON, including a best-effort category from a fixed taxonomy (null if nothing genuinely fits, e.g. a grocery receipt). Extraction calls for a batch run concurrently (bounded by `MAX_CONCURRENT_EXTRACTIONS` in `.env`) since each one is independent.
3. **Log** — every invoice is appended to the `Invoice_Log` sheet.
4. **Validate** — vendor match (resolves a `vendor_id`), category validity, math check, PO check, duplicate check.
5. **Review loop** — failures get flagged and emailed; a human approves fixes (e.g. a new vendor) by editing the sheet, and the agent re-validates on the next run.

Three layers guard against logging the same invoice twice — see
`references/validation_rules.md` and `SKILL.md`'s "Duplicate protection".

See `SKILL.md` for the full behavioral spec, and `references/` for exact
schemas and validation rules.

## Setup

### 1. Google Cloud

- Create (or reuse) a Google Cloud project
- Enable the **Gmail API**, **Drive API**, and **Sheets API**
- Create an **OAuth client** (Desktop app), save its JSON to `credentials/oauth-client.json`, and run `python scripts/authorize.py` once for the browser consent

> Gmail intake needs a real OAuth consent, not a service account — a plain service account cannot read a personal Gmail inbox at all.

### 2. Google Sheets

Nothing to create — the `Vendor_Master` and `Invoice_Log` Google Sheets are
created automatically (empty, with just the header row) the first time the
agent runs, inside a new "Agent Data" subfolder of the Drive folder pointed
to by `DRIVE_WATCH_FOLDER_ID`.

### 3. Environment

```bash
cp .env.example .env
# fill in OPENAI_API_KEY, DRIVE_WATCH_FOLDER_ID, NOTIFY_EMAIL, etc.
pip install -r requirements.txt
```

### 4. Run it

```bash
python scripts/main.py
```

Run this on a schedule (cron, Task Scheduler, or a CI job) — each run only processes what's new since last time.

### 5. Run the tests

```bash
python tests/test_validate_invoice.py
```

## The one design rule to keep

The agent never writes to the `Vendor_Master` sheet on its own beyond a
minimal `Pending`-status placeholder row. A flagged invoice only gets a
vendor added after a human sets `approve_vendor = TRUE` on its row in the
invoice log — and even then, a human still has to complete the vendor's
onboarding fields (country, tax ID, payment terms, etc.) directly in the
sheet before any downstream `Active`-only gate lets it flow through payment.
This is what keeps one bad OCR read from quietly polluting shared reference
data — see `SKILL.md` for more on this.
```

#### `SKILL.md`
```markdown
---
name: invoice-processing-agent
description: Use this skill whenever working on the invoice processing agent in this project — running the pipeline, debugging why an invoice failed validation, adding a new intake source, changing the sheet schema, or extending validation rules. Trigger on phrases like "process invoices", "run the invoice agent", "check for new invoices", "why did this invoice get flagged", or any request to modify extraction, validation, or the vendor approval flow.
---

# Invoice processing agent

This project watches for incoming invoices (Gmail attachments or a Drive folder), extracts structured data with OpenAI, logs every invoice to the `Invoice_Log` Google Sheet, validates it against the `Vendor_Master` Google Sheet, and routes failures to a human instead of guessing.

It keeps its own `Vendor_Master`/`Invoice_Log` Google Sheets in an "Agent Data" subfolder inside the watched `Invoice_Automation` Drive folder — it must never write into any other spreadsheet directly (see the design rule in `references/sheet_schema.md`).

## Pipeline (in order)

1. `scripts/intake_drive.py` / `scripts/intake_gmail.py` — pull new files, track what's already been processed in `state/`, and skip anything `scripts/dedup.py` recognizes as already-seen content. `intake_gmail.py` also archives each new attachment into the watched Drive folder's `Attachments from Gmail` subfolder.
2. `scripts/extract_invoice.py` — OpenAI (Responses API, Structured Outputs) reads the PDF/image and returns JSON matching `EXTRACTION_SCHEMA`/`TEXT_EXTRACTION_SCHEMA` (the prompt text is `EXTRACTION_PROMPT`/`TEXT_EXTRACTION_PROMPT` in that file), including a best-effort `category` from the fixed `AP_CATEGORIES` list (`config.py`) — null if nothing genuinely fits (e.g. a grocery receipt)
3. `scripts/validate_invoice.py::validate_invoice` — resolves `vendor_id` and checks: vendor match, category validity, math check, PO check, duplicate check (see `references/validation_rules.md`)
4. `scripts/sheets_client.py::append_invoice_row` — logs the row to the `Invoice_Log` sheet, stamping `logged_at`, collapsing multi-item receipts to one row (`qty_invoiced=1`, `unit_price=subtotal`), and rendering `line_items` as plain text (via `format_line_items()`), not JSON — see `references/sheet_schema.md`
5. `scripts/notify.py` — emails a human if validation failed
6. `scripts/main.py::apply_approved_vendors` — on the next run, picks up any row where a human set `approve_vendor = TRUE`, adds a **minimal** `Pending`-status vendor row to the `Vendor_Master` sheet (only `vendor_id`/`vendor_name`/`aliases` — everything else needs a human to complete it directly in the sheet), writes the new `vendor_id` back onto the triggering invoice row, and re-validates it

Run the whole thing with `python scripts/main.py`.

## Concurrency: extraction fan-out

`main.py::process_new_invoices` gathers every new file from both sources into one job list, then runs step 2 (the OpenAI call) through a `ThreadPoolExecutor` bounded by `MAX_CONCURRENT_EXTRACTIONS` (`.env`, default 4). This is safe specifically because each extraction call is stateless — no shared conversation, no context carried between invoices — so token cost per invoice is unaffected by how many run at once; only wall-clock time drops as batch size grows. Steps 3-5 (validate/append/notify) run back on the main thread, one invoice at a time, in the order extractions finish — this is deliberate: the duplicate check reads and appends to `existing_rows` in memory, and interleaving those writes across threads would create races. If you touch this loop, keep that split: parallel only around the OpenAI call, everything touching `existing_rows` or the sheet stays single-threaded.

`apply_approved_vendors` follows the same "load once, append locally" shape for `master_vendors`, for the same reason `existing_rows` is — avoiding a full-sheet re-read per row in the loop.

**If invoice volume grows into the hundreds-per-run range**, raising `MAX_CONCURRENT_EXTRACTIONS` further will start bumping into OpenAI per-minute rate limits rather than helping. At that point the right lever is switching `extract_invoice.py` to the [Batch API](https://platform.openai.com/docs/guides/batch) instead of thread-pooling synchronous calls — it's built for exactly this (bulk, non-interactive, no-latency-requirement extraction), runs at roughly half the per-token cost, and sidesteps rate limits entirely since it's not real-time. That's a real architecture change (submit a batch, poll or get a webhook, then run steps 3-5 over the results asynchronously rather than in the same process) — worth doing when volume actually justifies it, not preemptively.

## Duplicate protection

The same invoice can legitimately reach this pipeline more than once — it
gets emailed, and someone also drops a copy in the watched Drive folder;
two people upload the same file under different names; a run crashes
partway through and gets re-triggered. Three layers guard against actually
logging it twice, each catching what the one before it can't:

1. **Per-source ID ledgers** (`state/processed_gmail_ids.json`,
   `state/processed_drive_ids.json`) — skip a Gmail message ID or Drive
   file ID this specific source has already handled. Written incrementally,
   one item at a time, so a mid-run crash can't cause an already-finished
   item to replay on the next run. Gmail intake deliberately includes
   already-read mail (`GMAIL_QUERY` has no `is:unread`), so this ledger —
   not read/unread status — is what actually prevents reprocessing; nothing
   here ever modifies a message (scope is `gmail.readonly`, not `.modify`).
   How far back Gmail search even looks is a separate, narrower mechanism:
   `intake_gmail.py::_search_query()` appends an `after:` bound — from
   `EMAIL_CHECK_START_DATE` (`.env`, defaults to today) on the first-ever
   run, then from `state/gmail_last_checked.json` (a rolling checkpoint,
   second precision) on every run after that, so a run that checked at 3pm
   leaves the next run starting from 3pm onward. That checkpoint only
   advances after every matched message in a run finishes processing
   without error, so a mid-run crash re-covers the same window next time
   instead of silently skipping past unfinished messages — the id ledger
   above makes that safe, since anything already handled in that window
   just gets skipped again instantly.
2. **Content-hash ledger** (`state/processed_content_hashes.json`, via
   `scripts/dedup.py`) — skips a file whose exact bytes have already been
   processed, regardless of which source, filename, or Drive/Gmail ID it
   arrived under. This is the layer that specifically catches "emailed AND
   dropped in the Drive folder" and "two people uploaded the same PDF."
   It's a silent, mechanical skip (like the ID ledgers) — no sheet row, no
   notification — because it's the exact same bytes, not a judgment call.
3. **`duplicate_invoice` validation check** (`validate_invoice.py`) — the
   semantic safety net for when the bytes differ (a rescan, a re-export)
   but the extracted vendor + invoice_number (or vendor + invoice_date +
   total) match a row already in Invoice Log. Unlike layers 1–2, this
   **logs the row and flags `needs_review`, it does not skip** — two
   different invoices can coincidentally share a number, so a human makes
   the final call, same as every other validation check.

**If you (the AI assistant) are asked to check email or Drive for invoices
directly in chat — via Gmail/Drive tools rather than running
`python scripts/main.py`** — none of the three layers above apply; a chat
session has no ledger and doesn't persist one between conversations. Before
manually logging anything to the `Invoice_Log` sheet: read it first and
check the candidate invoice's vendor + invoice_number (or vendor +
invoice_date + total) against every existing row yourself, using the same
matching logic as `_find_duplicate` in `validate_invoice.py`. If it
matches, tell the user instead of adding a row. Prefer pointing the user at
running the real pipeline over manual chat-based logging whenever
possible — that's what actually has these guarantees built in. If you do
log manually, still follow `references/sheet_schema.md`: stamp `logged_at`
with the actual current date/time, resolve/assign `vendor_id` and
`category` rather than leaving them blank, and render `line_items` as
plain text, not JSON.

## The one rule that matters

**Never write to the Vendor_Master sheet except through the human-approval path** (`approve_vendor = TRUE` on a flagged row in the invoice log). If asked to "just auto-add new vendors" or similar, push back — that's the exact failure mode this design avoids, since it lets one bad OCR read silently create a fake vendor. If the user insists, that's their call to make explicitly, but flag the trade-off before making the change. Even on approval, this agent only ever writes a minimal `Pending` row — it never invents `country`, `tax_id`, `payment_terms`, or any other onboarding field it has no way of knowing from an invoice.

## Two separate sheets, owned by this agent alone

`Invoice_Log` and `Vendor_Master` are Google Sheets living in an "Agent
Data" subfolder inside the watched `Invoice_Automation` Drive folder
(`DRIVE_WATCH_FOLDER_ID` in `.env`) — created automatically, empty, the
first time they're read or written. **This agent must never open a
spreadsheet other than these two** — see the design rule in
`references/sheet_schema.md`. `sheets_client.py` writes by **column name**
against each sheet's real header row, never a hardcoded position — if
you're asked to add a field, add it to the column list in
`sheets_client.py`'s `values = {...}` dict.

## Where things live

- `references/sheet_schema.md` — exact columns for both sheets
- `references/validation_rules.md` — tolerances and matching logic, with the constants to tune
- `scripts/sheets_client.py` — the only module that reads/writes the two Google Sheets
- `scripts/dedup.py` — the shared content-hash ledger; see "Duplicate protection" above
- `state/gmail_last_checked.json` — rolling checkpoint (unix seconds) for how far back Gmail intake searches; see "Duplicate protection" above
- `tests/test_validate_invoice.py` — run with `python tests/test_validate_invoice.py`; extend this first when changing validation logic
- `.env.example` — every config value the scripts read, including `DRIVE_WATCH_FOLDER_ID`

## Common tasks

- **Adding a new validation check**: add a `_check_name(record) -> bool` function in `validate_invoice.py`, call it from `validate_invoice()`, add a test case, update `references/validation_rules.md`.
- **Adding a new intake source** (e.g. Slack uploads): follow the pattern in `intake_drive.py` — return a list of local file paths, keep a processed-ids ledger so nothing gets reprocessed by this source, AND call `dedup.already_seen()` / `dedup.mark_seen()` on each file's bytes so it's caught if the same file also arrives via Gmail or Drive.
- **Changing the extraction schema**: edit `EXTRACTION_PROMPT`/`EXTRACTION_SCHEMA` in `extract_invoice.py` and the `values = {...}` dict in `sheets_client.py::append_invoice_row` together — they need to stay in sync, or logging will silently drop fields. If the new field is a list (like `line_items`), give it a plain-text rendering the way `format_line_items()` does — don't just dump JSON into a cell.
- **Debugging a flagged invoice**: check the `issue` column on its row in the `Invoice_Log` sheet first — it names exactly which check failed. See `references/validation_rules.md` for what each one means and how to fix it.
```

#### `PIPELINE_REFERENCE.md`
```markdown
# Pipeline reference — validations, conditions, and design notes

A single consolidated reference for everything this agent checks, tolerates, or
enforces, and why. Detail for any one section also lives in `SKILL.md` /
`references/`; this file exists so it's all in one place to skim later.

## 1. Pipeline order

1. **Intake** — `intake_drive.py`, `intake_gmail.py`: find new files (and, for Gmail, new email-body text)
2. **Extract** — `extract_invoice.py`: OpenAI reads the file or email body text, returns structured JSON (including a best-effort `category` from a fixed taxonomy), or `null` if body text turns out not to be a real invoice
3. **Validate** — `validate_invoice.py`: resolves `vendor_id`, 5 checks described below
4. **Log** — `sheets_client.py::append_invoice_row`: one row per invoice in the `Invoice_Log` Google Sheet
5. **Notify** — `notify.py`: emails a human if any check failed
6. **Approve** — `main.py::apply_approved_vendors`: on the *next* run, picks up rows where a human set `approve_vendor = TRUE`, adds a minimal `Pending`-status vendor row, writes the new `vendor_id` back onto the row, re-validates

Run with `python scripts/main.py`. Meant to run on a schedule (cron / Task Scheduler) — each run only processes what's new since last time.

## 2. Validation checks (`validate_invoice.py`)

See `references/validation_rules.md` for the full table, tunable constants, and how to debug a flagged row.

## 3. Duplicate protection (3 layers, catching what the one before can't)

| Layer | Mechanism | Catches | Behavior on match |
|---|---|---|---|
| 1. Per-source ID ledger | `state/processed_gmail_ids.json`, `state/processed_drive_ids.json` — Gmail message ID / Drive file ID already handled | Re-seeing the exact same message/file from the exact same source | Silent skip, no row, no notification |
| 2. Content-hash ledger | `state/processed_content_hashes.json` via `dedup.py` (SHA-256 of raw bytes) | Same file emailed **and** dropped in Drive; same file under different filenames/IDs; sent in two different emails | Silent skip, no row, no notification |
| 3. `duplicate_invoice` validation check | Extracted vendor + invoice_number (or vendor + date + total) matches an existing Invoice Log row | Bytes differ but it's the same real invoice (rescan, re-export) | **Logged and flagged `needs_review`** — not skipped; human makes the call |

Written incrementally (one item at a time, not batched) so a mid-run crash can't cause an already-finished item to replay next run.

## 4. Gmail intake specifics (`intake_gmail.py`)

- **No `is:unread`** in `GMAIL_QUERY` — deliberate. Already-read mail is included; the per-source ID ledger (not read/unread status) is what prevents reprocessing.
- Scope is `gmail.readonly` for Gmail itself — nothing here ever modifies a message. If write behavior is ever added back, scope needs to change to `gmail.modify`. (Drive writes below use the separate `drive` scope.)
- **Search window** (`_search_query()`): first-ever run searches from `EMAIL_CHECK_START_DATE` (`.env`, defaults to **today** if unset — so a first run doesn't crawl entire mailbox history). Every run after that resumes from `state/gmail_last_checked.json`, a rolling unix-second checkpoint.
- Checkpoint only advances **after every matched message in the run finishes without error** — so a mid-run crash re-covers the same window next time (safe, since the ID ledger skips anything already handled instantly).
- A plain service account **cannot** read a personal Gmail inbox — needs either Workspace domain-wide delegation or a per-user OAuth flow (this project uses OAuth — see `auth.py`).
- **`GMAIL_QUERY` default is `subject:(invoice OR invoices)`, with no `has:attachment` requirement** — deliberate: a real invoice can also arrive typed or forwarded directly into the email body with no file attached.
- **Body-text extraction**: `fetch_new_invoice_sources()` returns one job per new attachment (`{"kind": "file", "path": ...}`) AND, separately, one job per new matched message's plain-text body (`{"kind": "text", "text": ..., "label": ...}`) — `_extract_body_text()` prefers `text/plain`, falling back to a tag-stripped `text/html` for messages with no plain-text part. `main.py` routes `"file"` jobs through `extract_invoice_data()` and `"text"` jobs through `extract_invoice_data_from_text()`. Because subject-line matching pulls in plenty of messages that just *mention* the word "invoice" (replies, marketing, forwarded threads with no amounts), `extract_invoice_data_from_text()` can return `None` — the model's `not_an_invoice` escape hatch — and `main.py` skips logging a row for those rather than hallucinating fields to fit the schema.

## 5. Drive intake specifics (`intake_drive.py`)

- Paginated via `nextPageToken` (page size 1000) — folders over ~100 files won't silently get truncated.
- "New" = Drive file ID not already in `state/processed_drive_ids.json`.
- Downloaded file is hash-checked against `dedup.py`; if already seen, the local copy is deleted immediately after download (not skipped pre-download, since Drive doesn't expose content hashes cheaply via this call).

## 6. Extraction (`extract_invoice.py`)

- Model: `gpt-4o` via the OpenAI **Responses API** (swap the `MODEL` constant for cost/accuracy tradeoffs) — reads PDFs natively (`input_file` content block, base64 data URL) and images (`input_image`) without a separate OCR/rasterize step
- **Structured Outputs** (`text.format = {"type": "json_schema", "strict": true, ...}`) enforce the schema server-side — `EXTRACTION_SCHEMA` / `TEXT_EXTRACTION_SCHEMA` in `extract_invoice.py` — instead of relying on prompt instructions + a fenced-code-block strip, so malformed JSON shouldn't happen in practice
- Accepted file types: `application/pdf`, `image/jpeg`, `image/png`, `image/webp` — anything else raises `ValueError`
- `max_output_tokens=4096` — headroom for long itemized receipts (raise further if a real invoice ever gets truncated mid-JSON)
- Extraction schema (`EXTRACTION_PROMPT` + `EXTRACTION_SCHEMA`): `vendor`, `invoice_number` (nullable — falls back to receipt/transaction number), `invoice_date` (`YYYY-MM-DD`), `line_items[]` (`description`, `quantity`, `unit_price`, `amount`), `subtotal`, `tax`, `total`, `po_number` (nullable), `po_line` (nullable, only if the document itself references one), `category` (nullable, must be exactly one of `AP_CATEGORIES` in `config.py` or `null`), `currency`
- Explicit instruction: **never invent a value not visibly on the document** — use `null`/`0` for genuinely absent fields, and never force `category` to the closest-sounding value when nothing genuinely fits
- If line items aren't itemized (simple receipt), the model returns one synthetic line item with the total amount
- The text-extraction path's schema (`TEXT_EXTRACTION_SCHEMA`) makes every field nullable, not just the ones nullable in the file-extraction schema — OpenAI's strict Structured Outputs mode requires every property to be present in the response even when `not_an_invoice` is `true` and there's nothing to fill in

## 7. Concurrency (`main.py::process_new_invoices`)

- Step 2 (OpenAI extraction) runs through a `ThreadPoolExecutor`, bounded by `MAX_CONCURRENT_EXTRACTIONS` (`.env`, default **4**)
- Safe because each extraction call is stateless — no shared conversation/context, so token cost per invoice is unaffected by concurrency; only wall-clock time drops
- Steps 3–5 (validate/append/notify) stay **single-threaded**, one invoice at a time, in the order extractions finish — deliberate, because the duplicate check reads/appends to `existing_rows` in memory and interleaving those writes across threads would race
- Same "load once, append locally" pattern applies to `master_vendors` in `apply_approved_vendors()`, to avoid a full-sheet re-read per pending row
- **If volume grows into hundreds-per-run**: raising `MAX_CONCURRENT_EXTRACTIONS` further will hit OpenAI per-minute rate limits before it helps — switch to the [Batch API](https://platform.openai.com/docs/guides/batch) instead (async, ~half per-token cost, sidesteps rate limits) rather than raising the pool size indefinitely

## 8. The one design rule that matters

**The agent never writes to the Vendor_Master sheet on its own beyond a minimal `Pending`-status placeholder row.** A flagged invoice only adds a vendor after a human sets `approve_vendor = TRUE` on its row in the invoice log, and only takes effect on the *next* run (`apply_approved_vendors`) — which writes just `vendor_id`, `vendor_name`, `aliases`, `status="Pending"`; every other onboarding field (country, tax ID, payment terms, criticality, bank details) is left blank because the extraction model has no way to know it from an invoice, and stays blank until a human completes it directly in the sheet. If you use a downstream system with its own `Active`-only gate, that keeps a `Pending` vendor from flowing into payment until then. This is what stops one bad OCR read from silently polluting shared reference data.

Known gap: `add_vendor()` always mints a brand-new `vendor_id` — it doesn't check whether the approved name is a near-match for an existing vendor row (e.g. approving "Costco Wholesale" when "COSTCO WHOLESALE" is already present) and merge it as an alias instead. Worth fixing if duplicate-ish vendor rows start piling up.

## 9. File schema

**Invoice_Log** and **Vendor_Master**, both Google Sheets in an "Agent Data" subfolder inside the watched `Invoice_Automation` Drive folder (`DRIVE_WATCH_FOLDER_ID`) — this agent's OWN sheets. Full column list: `references/sheet_schema.md`.

## 10. Config / `.env` parameters

| Variable | Default | Notes |
|---|---|---|
| `OPENAI_API_KEY` | — | required for extraction |
| `DRIVE_WATCH_FOLDER_ID` | — | required for Drive intake; also the parent of this agent's own "Agent Data" sheets folder |
| `GMAIL_QUERY` | `subject:(invoice OR invoices)` | intentionally no `is:unread`; matches subject line only, no attachment or label required — body text is extracted separately (§4) |
| `EMAIL_CHECK_START_DATE` | today (if unset) | only used on the very first run, before a checkpoint exists |
| `NOTIFY_EMAIL` | — | required for review alerts |
| `MAX_CONCURRENT_EXTRACTIONS` | `4` | raise cautiously, watch OpenAI rate limits |

`config.py` raises `RuntimeError` on a malformed `EMAIL_CHECK_START_DATE` (must be `YYYY-MM-DD`) and on any required value missing at the point it's actually used (`require()`).

## 11. Debugging a flagged row

See `references/validation_rules.md` — the `issue` column names exactly which check failed.
```

---

## Step 2 — Install dependencies

Run:
```bash
pip install -r requirements.txt
```

If `python`/`pip` aren't found, help me install Python 3.11+ first.

---

## Step 3 — Interview me for setup (one question at a time)

Now walk me through configuration conversationally. Ask ONE thing, wait for
my answer, act on it (write the file/edit the config), then move to the
next. Don't skip ahead or assume defaults I haven't confirmed.

1. **OpenAI API key.** Ask me for it. If I don't have one, tell me to get
   one at platform.openai.com/api-keys (with billing enabled) and wait.
   Once I give it to you, copy `.env.example` to `.env` and set
   `OPENAI_API_KEY` to my value.

2. **Google Cloud OAuth setup.** Walk me through this exactly, step by step,
   pausing for me to confirm each part is done:
   - Go to console.cloud.google.com, create a new project (or pick an
     existing one).
   - Under "APIs & Services > Library", enable: **Gmail API**, **Google
     Drive API**, **Google Sheets API**.
   - Under "APIs & Services > OAuth consent screen", set it up for
     **External** user type (unless I have Google Workspace, in which case
     Internal is fine) and add my own Google account as a test user if it's
     in testing mode.
   - Under "APIs & Services > Credentials", create an **OAuth client ID**,
     application type **Desktop app**. Download the resulting JSON.
   - Tell me to save that downloaded file as `credentials/oauth-client.json`
     in this project (create the `credentials/` folder if it doesn't
     exist).
   Confirm the file exists before moving on.

3. **Which Drive folder to watch.** Ask me: do I already have a Google
   Drive folder I want watched for invoices, or should I create a new one
   called `Invoice_Automation`? Once I've told you, run
   `python scripts/authorize.py` (this triggers the one-time browser sign-in
   — warn me a browser window will open and I need to approve access) and
   use its printed folder list to help me find the right folder's ID, or
   help me get the ID from the folder's Google Drive URL
   (`.../folders/<this-part-is-the-id>`). Write it to `DRIVE_WATCH_FOLDER_ID`
   in `.env`.

4. **Notify email.** Ask where "needs review" alerts should be sent. Write
   it to `NOTIFY_EMAIL` in `.env`.

5. **Expense categories.** Tell me the default category list this ships
   with (`Raw Materials, Packaging, MRO Supplies, Professional Services,
   Software & Subscriptions, Logistics & Freight, Facilities & Utilities`)
   and ask if I want to keep it or replace it with my own list. Whatever I
   answer, edit `AP_CATEGORIES` in `scripts/config.py` to match — don't
   leave the default in place without asking.

6. **Gmail search behavior (optional).** Tell me the default
   (`GMAIL_QUERY=subject:(invoice OR invoices)`, matches subject line only,
   already-read mail included) and ask if that's fine or if I want it
   narrower/broader. Only edit `.env` if I want something different.

7. **How far back to look on the first run (optional).** Explain
   `EMAIL_CHECK_START_DATE` defaults to today if left blank (so it won't
   crawl my whole mailbox). Ask if I want it to look further back, and if
   so from what date. Only set it if I say so.

Do not proceed to Step 4 until every required value above (`OPENAI_API_KEY`,
`DRIVE_WATCH_FOLDER_ID`, `NOTIFY_EMAIL`, and `credentials/oauth-client.json`)
is actually in place.

---

## Step 4 — Verify

1. Run `python tests/test_validate_invoice.py` — all 11 tests should pass.
   These are pure logic tests, no credentials required.
2. Confirm `credentials/token.json` exists (created by `authorize.py` in
   Step 3). If not, run `python scripts/authorize.py` now.
3. Tell me you're ready for a real end-to-end test, and ask me to either:
   - drop one real invoice (PDF, JPG, PNG, or WEBP) into the Drive folder
     we configured, or
   - give you a local file path to a sample invoice/receipt.
   Then run `python scripts/main.py` and show me the result. Check the
   `Invoice_Log` sheet (link is printed by `notify.py`/`sheets_client.py`,
   or open the "Agent Data" subfolder in the watched Drive folder) so I can
   see the logged row. If it comes back `needs_review`, that's normal —
   explain what the `issue` column says and why it's a feature, not a bug
   (see `references/validation_rules.md`).

---

## Step 5 — Wrap up

Tell me:
- Setup is complete and how to run it again any time: `python scripts/main.py`
- How to put it on a schedule (Windows Task Scheduler if I'm on Windows,
  cron if I'm on Mac/Linux) so it checks automatically, and offer to help
  me set that up if I want it now.
- That `.env` and `credentials/` hold secrets and must never be shared or
  committed to a public repo (already covered by `.gitignore`).
- Where to look when something needs attention: `SKILL.md` for the full
  behavioral spec, `references/validation_rules.md` for what a flagged
  `issue` means and how to fix it, `PIPELINE_REFERENCE.md` for everything
  else.
