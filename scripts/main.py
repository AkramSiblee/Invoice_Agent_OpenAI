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


def _extract(kind, payload, label):
    if kind == "file":
        return extract_invoice_data(payload)
    return extract_invoice_data_from_text(payload, label)


def process_jobs(jobs, master_vendors, existing_rows):
    """Runs extract -> validate -> log -> notify for a list of jobs.

    Each job is (source_name, kind, payload, label): kind "file" means
    payload is a local path (extract_invoice_data); kind "text" means
    payload is an email body string (extract_invoice_data_from_text).
    label is what gets logged/printed — the filename for "file" jobs, the
    subject/message-id description intake_gmail.py built for "text" jobs.

    `master_vendors` and `existing_rows` are mutated in place (existing_rows
    grows as rows are appended) so a caller can invoke this repeatedly across
    several smaller batches within one run and still catch cross-batch
    duplicates.

    Extraction is the slow, token-spending step (one OpenAI call per file),
    and each call is fully independent — no shared conversation, no state
    carried between invoices — so token cost per invoice stays flat
    regardless of batch size. That independence is exactly what makes it
    safe to fan these calls out to a bounded pool of concurrent workers:
    wall-clock time on a large batch drops from O(n) sequential API round
    trips to roughly O(n / MAX_CONCURRENT_EXTRACTIONS), which is what
    actually matters as invoice volume grows.

    Everything after extraction (validate, append, notify) stays
    single-threaded on the main thread: it's cheap local logic, not an API
    call, and the duplicate check depends on existing_rows being updated
    one invoice at a time — parallelizing it would just add races for no
    benefit.
    """
    if not jobs:
        return

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


def process_new_invoices():
    master_vendors = get_master_vendors()
    # Loaded once and appended to in-memory as we go, so two copies of the
    # same invoice arriving from different sources in the same run (e.g.
    # emailed AND dropped in the watched Drive folder) still catch each other,
    # not just invoices that were already in the sheet before this run.
    existing_rows = get_invoice_log_rows()

    jobs = [("drive", "file", file_path, file_path) for file_path in intake_drive.fetch_new_files()]
    for source in intake_gmail.fetch_new_invoice_sources():
        if source["kind"] == "file":
            jobs.append(("email", "file", source["path"], source["path"]))
        else:
            jobs.append(("email", "text", source["text"], source["label"]))
    process_jobs(jobs, master_vendors, existing_rows)


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
