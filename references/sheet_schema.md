# File schema

This agent keeps its **own** `Vendor_Master` and `Invoice_Log` Google Sheets
in a dedicated **"Agent Data"** subfolder inside the watched
`Invoice_Automation` Drive folder (`DRIVE_WATCH_FOLDER_ID` in `.env`) —
schema-matched to the Accounts Payable Agent's own files
(`G:\My Drive\accounts-payable-agent\`) so rows can be copied across by a
human, but a **separate copy**, not the same physical file.
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

An earlier version of this agent pointed directly at the AP Agent's own
`Sample_Invoice_Log.xlsx` on its Drive folder. During cleanup after a bug,
a row-count mistake deleted one of that agent's real rows — its own
hand-crafted data (test scenarios, or however it was populated), not
anything this agent had written. The row could not be confidently
reconstructed. **This agent must never open, read, or write any spreadsheet
other than its own two, in its own "Agent Data" folder, again.** If you're
asked to make this agent write into the AP Agent's files directly, push
back — that's the exact mistake this architecture exists to prevent.
Getting invoices into AP's real pipeline is a human copy-paste step (or a
separate, deliberate sync tool, if ever built) — not something this agent
does automatically.

## Invoice_Log

One row per invoice. The first 11 columns mirror the AP Agent's own
required schema (`accounts-payable-agent/scripts/excel_loader.py`) exactly,
so a row can be copied into AP's file with no reshaping. Everything after
`invoice_date` is this agent's own addition.

| Column | Type | Notes |
|---|---|---|
| invoice_number | text | receipt/transaction number if there's no formal invoice number |
| vendor_id | text | resolved against Vendor_Master — `V-XXXX`. Blank when `vendor_not_found` |
| vendor_name | text | as extracted |
| po_number | text | blank if none |
| po_line | number | blank unless the document itself references a specific PO line |
| category | text | must be exactly one of `AP_CATEGORIES` (`config.py`), or blank if nothing genuinely fits — e.g. a grocery/retail receipt. Blank flags `category_unresolved` |
| qty_invoiced | number | Total quantity, filled only when every line shares one unit price and qty × price reproduces the line amounts and subtotal (`sheets_client.py::confirmed_qty_and_price()`); blank otherwise — see `line_items` below |
| unit_price | number | The common per-unit price under the same confirmation rule; blank when not confirmed |
| total | number | |
| currency | text | e.g. `USD`, `CAD` — only when the document states it; blank for a bare `$` |
| invoice_date | date | |
| subtotal | number | AP's own schema has no tax field — this and `tax` are this agent's addition |
| tax | number | |
| date_received | date | when the file was picked up |
| logged_at | datetime | set once, at append time |
| source | text | `drive` or `email` |
| file_name | text | original file name |
| line_items | text | plain text, not JSON: `description ($amount); description ($amount)`. The only place a mixed-price receipt's itemization survives — `qty_invoiced`/`unit_price` stay blank when the lines don't share one price. Built by `sheets_client.py::format_line_items()` |
| review_status | text | `verified` or `needs_review` |
| issue | text | blank, or a `;`-separated list of which checks failed |
| approve_vendor | boolean | human sets to `TRUE` to approve adding a new vendor found on this row |

## Vendor_Master

The AP Agent's full 15-column schema, used as-is.

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
column is left blank — the extraction model has no way to know these from an invoice.
`status="Pending"` matters if a row is ever copied into the AP Agent's real
file: its own `Active`-only gate keeps a `Pending` vendor from flowing into
payment until a human completes the row.

## Both sheets are created empty on first use

Unlike the AP Agent's files, there's no pre-existing data here to be careful
around — `sheets_client.py::_get_or_create_sheet()` creates each sheet
(and the "Agent Data" folder itself) with just the header row the first
time it's needed, if it doesn't already exist.
