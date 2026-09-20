"""
One command for the everyday routine: find new invoice files in the watched
Drive tree, rename each to Vendor_YYYY-MM-DD-InvoiceNumber.ext, then run the
normal pipeline (Drive + Gmail intake -> extract -> validate -> log -> notify).

Renaming happens BEFORE logging so the Invoice Log's file_name column records
the proper name. Only files not yet in the processed ledger are read, so this
costs two GPT-4o reads per NEW file rather than per file in the folder (which
is what rename_invoices.py on the whole tree would do).

Emailed attachments are the exception: Gmail intake archives them into the
"Attachments from Gmail" Drive folder DURING the pipeline, after the first
rename pass has already run. So a second pass runs afterwards over that folder,
renames any archived copy, and updates the matching Invoice Log row's
file_name (matched on the old name, and only when exactly one row matches).
Those copies are then marked processed in the Drive ledger, since they were
already extracted from the email.

A file is renamed only when rename_invoices.analyse() finds both reads in
agreement and the date plausible; otherwise it keeps its name, is reported as
REVIEW, and is still logged by the pipeline.

Usage: python scripts/run_new_invoices.py [--dry-run]
"""
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import intake_drive
import intake_gmail
import rename_invoices
import sheets_client
from config import DRIVE_WATCH_FOLDER_ID, require
from main import process_new_invoices, apply_approved_vendors


def _rename_files(service, files: list[dict], taken: set, dry_run: bool) -> list[tuple[str, str]]:
    """Reads each file twice and renames the ones both reads agree on.

    Returns [(old_name, new_name)] for files actually renamed. `taken` is the
    set of names already in use and is added to as names are claimed."""
    def work(f):
        try:
            return rename_invoices.analyse(f)
        except Exception as e:  # keep going; report per file
            return {"file": f, "error": str(e)}

    with ThreadPoolExecutor(max_workers=rename_invoices.WORKERS) as pool:
        results = list(pool.map(work, files))

    renamed = []
    for r in results:
        f = r["file"]
        if r.get("error"):
            print(f"[error]   {f['name']}: {r['error']}")
            continue
        if r["problems"]:
            print(f"[REVIEW]  {f['name']}  (would be {r['new_name']})  -- " + "; ".join(r["problems"]))
            continue
        if r["new_name"] == f["name"]:
            continue  # already properly named

        new_path = Path(r["new_name"])
        new_name, n = r["new_name"], 1
        while new_name in taken:
            n += 1
            new_name = f"{new_path.stem}_{n}{new_path.suffix}"
        taken.add(new_name)

        print(f"{'[dry-run]' if dry_run else '[renamed]'} {f['name']}  ->  {new_name}")
        if not dry_run:
            service.files().update(fileId=f["id"], body=dict(name=new_name)).execute()
            renamed.append((f["name"], new_name))
    return renamed


def rename_new_files(dry_run: bool) -> None:
    """Pass 1 (before the pipeline): every new file in the watched tree."""
    root_id = require(DRIVE_WATCH_FOLDER_ID, "DRIVE_WATCH_FOLDER_ID")
    service = rename_invoices._service()
    rename_invoices.TMP_DIR.mkdir(parents=True, exist_ok=True)

    ledger = intake_drive._load_ledger()
    all_files = list(rename_invoices._walk_files(service, root_id))
    new_files = [f for f in all_files if f["id"] not in ledger]
    if not new_files:
        print("No new Drive files to rename.")
        return

    print(f"{len(new_files)} new Drive file(s) to rename.")
    _rename_files(service, new_files, {f["name"] for f in all_files}, dry_run)


def plan_log_updates(rows: list[dict], renamed: list[tuple[str, str]]) -> tuple[list[tuple[int, str]], list[str]]:
    """Which Invoice Log rows get a new file_name.

    Returns ([(row_number, new_name)], [warning]). A rename is applied only
    when exactly one row carries the old name; zero or several matches are
    reported instead of guessed at."""
    updates, warnings = [], []
    for old_name, new_name in renamed:
        matches = [r for r in rows if r.get("file_name") == old_name]
        if len(matches) == 1:
            updates.append((matches[0]["_row_number"], new_name))
        else:
            warnings.append(f"log not updated for {old_name!r} -> {new_name!r}: {len(matches)} row(s) carry that file_name")
    return updates, warnings


def rename_emailed_attachments(dry_run: bool) -> None:
    """Pass 2 (after the pipeline): archived copies of emailed attachments."""
    root_id = require(DRIVE_WATCH_FOLDER_ID, "DRIVE_WATCH_FOLDER_ID")
    service = rename_invoices._service()
    rename_invoices.TMP_DIR.mkdir(parents=True, exist_ok=True)

    ledger = intake_drive._load_ledger()
    archive_id = intake_gmail._get_archive_folder_id(service)
    archived = [f for f in rename_invoices._walk_files(service, archive_id) if f["id"] not in ledger]
    if not archived:
        print("No new emailed attachments to rename.")
        return

    print(f"{len(archived)} new emailed attachment(s) to rename.")
    taken = {f["name"] for f in rename_invoices._walk_files(service, root_id)}
    renamed = _rename_files(service, archived, taken, dry_run)
    if dry_run:
        return

    updates, warnings = plan_log_updates(sheets_client.get_invoice_log_rows(), renamed)
    sheet_id = sheets_client._invoice_log_id()
    for row_number, new_name in updates:
        sheets_client._update_cell(sheet_id, sheets_client.INVOICE_LOG_COLUMNS, row_number, "file_name", new_name)
        print(f"[log]     row {row_number} file_name -> {new_name}")
    for warning in warnings:
        print(f"[warning] {warning}")

    # Already extracted and logged from the email itself; don't let the next
    # run treat these archived copies as new Drive files.
    intake_drive._save_ledger(ledger | {f["id"] for f in archived})


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    rename_new_files(dry_run)
    if dry_run:
        print("Dry run: skipping the logging pipeline and the emailed-attachment pass.")
    else:
        process_new_invoices()
        apply_approved_vendors()
        rename_emailed_attachments(dry_run=False)
