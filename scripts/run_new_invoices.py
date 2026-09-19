"""
One command for the everyday routine: find new invoice files in the watched
Drive tree, rename each to Vendor_YYYY-MM-DD-InvoiceNumber.ext, then run the
normal pipeline (Drive + Gmail intake -> extract -> validate -> log -> notify).

Renaming happens BEFORE logging so the Invoice Log's file_name column records
the proper name. Only files not yet in the processed ledger are read, so this
costs two GPT-4o reads per NEW file rather than per file in the folder (which
is what rename_invoices.py on the whole tree would do).

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
import rename_invoices
from config import DRIVE_WATCH_FOLDER_ID, require
from main import process_new_invoices, apply_approved_vendors


def rename_new_files(dry_run: bool) -> None:
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

    def work(f):
        try:
            return rename_invoices.analyse(f)
        except Exception as e:  # keep going; report per file
            return {"file": f, "error": str(e)}

    with ThreadPoolExecutor(max_workers=rename_invoices.WORKERS) as pool:
        results = list(pool.map(work, new_files))

    taken = {f["name"] for f in all_files}
    for r in results:
        f = r["file"]
        if r.get("error"):
            print(f"[error]   {f['name']}: {r['error']}")
            continue
        if r["problems"]:
            print(f"[REVIEW]  {f['name']}  (would be {r['new_name']})  -- " + "; ".join(r["problems"]))
            continue

        new_path = Path(r["new_name"])
        new_name, n = r["new_name"], 1
        while new_name in taken:
            n += 1
            new_name = f"{new_path.stem}_{n}{new_path.suffix}"
        taken.add(new_name)

        print(f"{'[dry-run]' if dry_run else '[renamed]'} {f['name']}  ->  {new_name}")
        if not dry_run:
            service.files().update(fileId=f["id"], body={"name": new_name}).execute()


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    rename_new_files(dry_run)
    if dry_run:
        print("Dry run: skipping the logging pipeline.")
    else:
        process_new_invoices()
        apply_approved_vendors()
