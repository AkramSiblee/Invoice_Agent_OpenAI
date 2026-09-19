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


def fetch_new_files(root_id: str | None = None) -> list[str]:
    """Downloads any new files in the watched tree and returns local paths.

    `root_id` scopes the walk to a specific subfolder (e.g. a one-off run
    against a single newly-added client folder) instead of the full watched
    tree. Defaults to `DRIVE_WATCH_FOLDER_ID`. The per-source ledger and
    dedup layers below apply the same way regardless of scope, so a file
    seen via a scoped run is still correctly skipped by a later full run.

    Two layers keep this from double-processing the same invoice:
    1. `processed` (this file's ledger) skips a Drive file ID we've already
       handled in a previous run.
    2. `dedup.py` skips a file whose exact byte content has already been
       processed under ANY source — this is what catches the same PDF
       already archived by `intake_gmail.py` and also dropped by hand into
       a month folder.
    """
    root_id = root_id or require(DRIVE_WATCH_FOLDER_ID, "DRIVE_WATCH_FOLDER_ID")
    service = _service()
    processed = _load_ledger()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    new_paths = []
    for f, folder_path in _walk(service, root_id):
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
