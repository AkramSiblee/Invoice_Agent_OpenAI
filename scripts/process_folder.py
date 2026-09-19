"""
One-off runner: processes new invoices from a single Drive subfolder only,
instead of the whole watched tree. Useful when a new client/vendor folder
shows up under 'Invoice Inbox' and you want to run just that folder through
the pipeline (e.g. to sanity-check results) without also sweeping up
whatever else happens to be new elsewhere in the tree.

Downloads all new files under the given folder up front (cheap, no OpenAI
cost — same ledger/dedup guarantees as a normal run), then runs extract ->
validate -> log -> notify in small batches so progress is visible and a
batch's OpenAI/email cost is bounded, rather than firing every file at once.

Usage: python scripts/process_folder.py <drive_folder_id> [--batch-size N]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import intake_drive
from main import process_jobs
from sheets_client import get_master_vendors, get_invoice_log_rows


def process_folder(folder_id: str, batch_size: int = 5):
    file_paths = intake_drive.fetch_new_files(root_id=folder_id)
    if not file_paths:
        print("No new files found in this folder.")
        return

    print(f"Found {len(file_paths)} new file(s). Processing in batches of {batch_size}.")

    master_vendors = get_master_vendors()
    existing_rows = get_invoice_log_rows()

    for i in range(0, len(file_paths), batch_size):
        batch = file_paths[i : i + batch_size]
        print(f"\n--- Batch {i // batch_size + 1}: {len(batch)} file(s) ---")
        jobs = [("drive", "file", path, path) for path in batch]
        process_jobs(jobs, master_vendors, existing_rows)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/process_folder.py <drive_folder_id> [--batch-size N]")
        sys.exit(1)

    folder_id = sys.argv[1]
    batch_size = 5
    if "--batch-size" in sys.argv:
        batch_size = int(sys.argv[sys.argv.index("--batch-size") + 1])

    process_folder(folder_id, batch_size)
