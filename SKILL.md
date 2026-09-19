---
name: invoice-processing-agent
description: Use this skill whenever working on the invoice processing agent in this project — running the pipeline, debugging why an invoice failed validation, adding a new intake source, changing the sheet schema, or extending validation rules. Trigger on phrases like "process invoices", "run the invoice agent", "check for new invoices", "why did this invoice get flagged", or any request to modify extraction, validation, or the vendor approval flow.
---

# Invoice processing agent

This project watches for incoming invoices (Gmail attachments or a Drive folder), extracts structured data with OpenAI, logs every invoice to the `Invoice_Log` Google Sheet, validates it against the `Vendor_Master` Google Sheet, and routes failures to a human instead of guessing.

**This agent's file schema is matched to a separate Accounts Payable Agent's own files** so rows can be copied across, but it keeps its own `Vendor_Master`/`Invoice_Log` Google Sheets in an "Agent Data" subfolder inside the watched `Invoice_Automation` Drive folder — it must never write into the AP Agent's own files directly (see the incident note in `references/sheet_schema.md` for why this rule exists).

## Pipeline (in order)

1. `scripts/intake_drive.py` / `scripts/intake_gmail.py` — pull new files, track what's already been processed in `state/`, and skip anything `scripts/dedup.py` recognizes as already-seen content. `intake_gmail.py` also archives each new attachment into the watched Drive folder's `Attachments from Gmail` subfolder itself (no external Zapier automation needed for that anymore).
2. `scripts/extract_invoice.py` — OpenAI (Responses API, Structured Outputs) reads the PDF/image and returns JSON matching `EXTRACTION_SCHEMA`/`TEXT_EXTRACTION_SCHEMA` (the prompt text is `EXTRACTION_PROMPT`/`TEXT_EXTRACTION_PROMPT` in that file), including a best-effort `category` from AP's fixed `AP_CATEGORIES` list (`config.py`) — null if nothing genuinely fits (e.g. a grocery receipt)
3. `scripts/validate_invoice.py::validate_invoice` — resolves `vendor_id` and checks: vendor match, category validity, math check, PO check, duplicate check (see `references/validation_rules.md`)
4. `scripts/sheets_client.py::append_invoice_row` — logs the row to the `Invoice_Log` sheet, stamping `logged_at`, filling `qty_invoiced`/`unit_price` only when the line items confirm them (blank otherwise, never defaulted to 1 × subtotal), and rendering `line_items` as plain text (via `format_line_items()`), not JSON — see `references/sheet_schema.md`
5. `scripts/notify.py` — emails a human if validation failed
6. `scripts/main.py::apply_approved_vendors` — on the next run, picks up any row where a human set `approve_vendor = TRUE`, adds a **minimal** `Pending`-status vendor row to the `Vendor_Master` sheet (only `vendor_id`/`vendor_name`/`aliases` — everything else needs a human to complete it directly in the sheet before AP's `Active`-only gate will let it through), writes the new `vendor_id` back onto the triggering invoice row, and re-validates it

Run the whole thing with `python scripts/main.py`.

## Concurrency: extraction fan-out

`main.py::process_new_invoices` gathers every new file from both sources into one job list, then runs step 2 (the OpenAI call) through a `ThreadPoolExecutor` bounded by `MAX_CONCURRENT_EXTRACTIONS` (`.env`, default 4). This is safe specifically because each extraction call is stateless — no shared conversation, no context carried between invoices — so token cost per invoice is unaffected by how many run at once; only wall-clock time drops as batch size grows. Steps 3-5 (validate/append/notify) run back on the main thread, one invoice at a time, in the order extractions finish — this is deliberate, not an oversight: the duplicate check reads and appends to `existing_rows` in memory, and interleaving those writes across threads would create races. If you touch this loop, keep that split: parallel only around the Claude call, everything touching `existing_rows` or the sheet stays single-threaded.

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

**If you (Claude) are asked to check email or Drive for invoices directly
in chat — via the Gmail/Drive MCP tools rather than running
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
first time they're read or written. Their schema matches the Accounts
Payable Agent's own files column-for-column (so a row can be copied across
by hand), but **this agent must never open a spreadsheet other than these
two** — see the incident note in `references/sheet_schema.md`.
`sheets_client.py` writes by **column name** against each sheet's real
header row, never a hardcoded position — if you're asked to add a field,
add it to the column list in `sheets_client.py`'s `values = {...}` dict.

## Where things live

- `references/sheet_schema.md` — exact columns for both sheets, and which ones AP's own pipeline reads
- `references/validation_rules.md` — tolerances and matching logic, with the constants to tune
- `scripts/sheets_client.py` — the only module that reads/writes the two Google Sheets
- `scripts/dedup.py` — the shared content-hash ledger; see "Duplicate protection" above
- `state/gmail_last_checked.json` — rolling checkpoint (unix seconds) for how far back Gmail intake searches; see "Duplicate protection" above
- `tests/test_validate_invoice.py` — run with `python tests/test_validate_invoice.py`; extend this first when changing validation logic
- `.env.example` — every config value the scripts read, including `DRIVE_WATCH_FOLDER_ID`

## Common tasks

- **Adding a new validation check**: add a `_check_name(record) -> bool` function in `validate_invoice.py`, call it from `validate_invoice()`, add a test case, update `references/validation_rules.md`.
- **Adding a new intake source** (e.g. Slack uploads): follow the pattern in `intake_drive.py` — return a list of local file paths, keep a processed-ids ledger so nothing gets reprocessed by this source, AND call `dedup.already_seen()` / `dedup.mark_seen()` on each file's bytes so it's caught if the same file also arrives via Gmail or Drive.
- **Changing the extraction schema**: edit `EXTRACTION_PROMPT` in `extract_invoice.py` and the `values = {...}` dict in `sheets_client.py::append_invoice_row` together — they need to stay in sync, or logging will silently drop fields. If the new field is a list (like `line_items`), give it a plain-text rendering the way `format_line_items()` does — don't just dump JSON into a cell. **Never rename or remove one of the 11 AP-required columns** without checking `accounts-payable-agent/scripts/excel_loader.py` — that pipeline reads this file too.
- **Debugging a flagged invoice**: check the `issue` column on its row in the `Invoice_Log` sheet first — it names exactly which check failed (`vendor_not_found`, `category_unresolved`/`category_invalid`, `math_mismatch`, `po_malformed`, `po_not_found`, or `duplicate_invoice`). For `vendor_not_found`, check `Vendor_Master` — the extracted vendor string has to match a `vendor_name` or `aliases` entry there within the fuzzy-match cutoff in `validation_rules.md`; a legitimate vendor with wording that's too different (e.g. "Costco Wholesale" vs. a stored "COSTCO") will flag even though it's not really wrong. For `category_unresolved`, that's often correct, not a bug — a retail/grocery receipt genuinely has no home in AP's 7-category list, and it should stay parked in `needs_review` rather than get a forced guess.
