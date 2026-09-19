"""
One-off tool: renames scanned invoice/receipt files inside a Google Drive
folder (recursively) to Vendor_YYYY-MM-DD-InvoiceNumber.ext.

Unlike the main pipeline's extraction, this uses a small, naming-only prompt
and reads every file TWICE (the native PDF, then a high-res page render). A
file is renamed automatically only when both reads agree on vendor, date and
invoice number AND the date is plausible relative to when the file was
uploaded to Drive. Anything else is left untouched and reported as REVIEW so a
human (or a follow-up pass) can check it against the actual document.

Vendor names are normalized (drops "The", store numbers, legal suffixes, and
folds known aliases such as SHELL / Shell Canada Products -> Shell) so the same
merchant always gets the same spelling.

This does NOT touch state/processed_drive_ids.json or dedup.py -- it only
reads and renames files in Drive, so a file renamed here is still picked up
normally by main.py / process_folder.py afterward.

Usage:
  python scripts/rename_invoices.py <drive_folder_id> [--dry-run] [--limit N]
"""
import base64
import io
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from openai import OpenAI

from auth import get_credentials
from config import OPENAI_API_KEY, require
from extract_invoice import _file_content_block, _media_type

FOLDER_MIME = "application/vnd.google-apps.folder"
SUPPORTED_MIMES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
}
TMP_DIR = Path(__file__).resolve().parent.parent / "downloads" / "_rename_tmp"
MODEL = os.environ.get("RENAME_MODEL", "gpt-4o")
WORKERS = 4

# A receipt dated more than this long before the scan was uploaded is far more
# likely a misread year (2025 -> 2023) than a genuinely old document.
MAX_AGE_DAYS = 730
# Receipts can't be dated after the day they were scanned (+ slack for timezones).
FUTURE_SLACK_DAYS = 2

NAMING_PROMPT = """You are reading a scanned store receipt or vendor invoice. Extract ONLY these fields, reading every digit slowly and carefully (scans are often faint):

- vendor: the brand/merchant name as a customer knows it (e.g. "Costco", "The Home Depot", "Shell", "A&W", "Punjab Sweets"). NO store number, city, street, franchise owner, legal suffix (Ltd, Inc, Corp) or tagline.
- date_printed: the transaction/invoice date exactly as printed on the document (raw text). If several dates are printed (order date, due date, print time), use the transaction/invoice date, not the due date.
- invoice_date: that same date as YYYY-MM-DD. Two-digit years mean 20YY. These documents were scanned in September 2026 and are almost always dated 2024-2026, so if you are about to write 2023 or earlier, re-read the year digits first. For ambiguous numeric dates like 03/04/25, use the format the receipt itself uses elsewhere (e.g. a printed month name); Canadian POS receipts are usually YYYY/MM/DD or MM/DD/YY, but Home Depot prints DD/MM/YY (day first: "07/08/25" is 7 August 2025; its barcode line and "policy expires" date confirm this). Never guess: null if no date is legible.
- invoice_number: choose in this priority order, and use the FIRST kind that is printed on the document:
    1. a number labeled Invoice No / Invoice # / Invoice ID (this always wins, even when a "Trans #" or other number is also printed),
    2. a number labeled Receipt # (this outranks Trans # -- e.g. "Receipt #:09784975" beats "Tran #: 326"), then Trans # / Transaction # / Sale # / Check #,
    3. a unique receipt identifier such as "YOUR ID: ..." printed near the survey/footer,
    4. only as a last resort, an Order # (order numbers are small counters that reset daily, e.g. "Order 08"),
    5. for Home Depot, the barcode line reads "STORE REG TRANS DATE TIME" (e.g. "7065 01 43248 07/08/2025 0544"): use the TRANS number only (43248).
  Do NOT use phone numbers, GST/tax registration numbers, card digits, auth codes, card-payment "Reference #" numbers, terminal/register numbers, survey codes, PO/job/customer numbers, or a STORE number -- a number printed in the header directly under the store name/address (e.g. "Tim Hortons # 103738", "RONA+ Edmonton S. Common 82952", "Store#: 40745") is a store number. null if no such number exists.
- number_label: the label printed next to invoice_number (null if invoice_number is null).
"""

NAMING_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": {"type": "string"},
        "date_printed": {"type": ["string", "null"]},
        "invoice_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "invoice_number": {"type": ["string", "null"]},
        "number_label": {"type": ["string", "null"]},
    },
    "required": ["vendor", "date_printed", "invoice_date", "invoice_number", "number_label"],
    "additionalProperties": False,
}

# (regex on the lowercase vendor key -- letters, digits and "&" only, first
# match wins, canonical name). Add a line here whenever a new merchant shows up
# under two spellings.
VENDOR_ALIASES = [
    (r"^shell", "Shell"),
    (r"^petrocanada", "PetroCanada"),
    (r"^costco", "Costco"),
    (r"^(the)?homedepot", "HomeDepot"),
    (r"^mcdonald", "McDonalds"),
    (r"^canadiantire", "CanadianTire"),
    (r"^punjabsweets", "PunjabSweets"),
    (r"^premiumfloors", "PremiumFloors"),
    (r"^snowbird", "SnowbirdRentals"),
    (r"^rona(?!ld)", "Rona"),
    (r"^timhortons", "TimHortons"),
    (r"^ikea", "IKEA"),
    (r"^(a&w|aw|awrestaurants?)$", "AW"),
    (r"^freshco", "FreshCo"),
    (r"^dollarama", "Dollarama"),
    (r"^(northcentral)?coop(?!er)", "NorthCentralCoop"),
    (r"^courtofjustice", "CourtOfJustice"),
    (r"^shoppers", "ShoppersDrugMart"),
    (r"^princessauto", "PrincessAuto"),
    (r"^greatclips", "GreatClips"),
    (r"^carlsjr", "CarlsJr"),
    (r"^burgerking", "BurgerKing"),
    (r"^redswan", "RedSwanPizza"),
]
_LEGAL_SUFFIX = re.compile(r"(ltd|limited|inc|incorporated|corp|corporation|llc|co|company|products)")


def _service():
    return build("drive", "v3", credentials=get_credentials())


def _walk_files(service, root_id):
    """Breadth-first walk of the folder tree, yielding file dicts."""
    queue = [root_id]
    seen_folders = {root_id}
    while queue:
        folder_id = queue.pop(0)
        page_token = None
        while True:
            response = service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, createdTime)",
                pageSize=1000,
                pageToken=page_token,
            ).execute()
            for f in response.get("files", []):
                if f["mimeType"] == FOLDER_MIME:
                    if f["id"] not in seen_folders:
                        seen_folders.add(f["id"])
                        queue.append(f["id"])
                elif f["mimeType"] in SUPPORTED_MIMES:
                    yield f
            page_token = response.get("nextPageToken")
            if not page_token:
                break


def _download(service, file_id, dest):
    request = service.files().get_media(fileId=file_id)
    with io.FileIO(dest, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


# --- vendor / invoice-number normalization ---------------------------------

def canonical_vendor(raw: str | None) -> str:
    raw = (raw or "").strip()
    if not raw:
        return "UnknownVendor"
    key = re.sub(r"[^a-z0-9&]+", "", raw.lower())
    for pattern, canonical in VENDOR_ALIASES:
        if re.search(pattern, key):
            return canonical
    # Unknown vendor: drop a leading "The" and trailing legal suffixes, then
    # PascalCase (ALL-CAPS input is title-cased so it doesn't shout).
    words = re.sub(r"[^A-Za-z0-9 ]+", " ", raw.replace("&", "")).split()
    while words and words[0].lower() == "the":
        words.pop(0)
    while words and _LEGAL_SUFFIX.fullmatch(words[-1].lower()):
        words.pop()
    name = "".join(w.capitalize() if w.isupper() else w[0].upper() + w[1:] for w in words)
    return name or "UnknownVendor"


def _clean_invnum(inv_num: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "", inv_num or "").strip("-")
    return cleaned or "no-invnum"


def _norm(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


# --- extraction -------------------------------------------------------------

def _read(client, content) -> dict:
    response = client.responses.create(
        model=MODEL,
        temperature=0,
        input=[{"role": "user", "content": [*content, {"type": "input_text", "text": NAMING_PROMPT}]}],
        max_output_tokens=600,
        text={"format": {"type": "json_schema", "name": "naming_extraction", "schema": NAMING_SCHEMA, "strict": True}},
    )
    return json.loads(response.output_text)


def _rendered_blocks(path: Path, max_pages: int = 3) -> list[dict]:
    """High-res page renders -- a genuinely different view of the document
    from the PDF-native read, so the two reads make independent errors."""
    if _media_type(str(path)) != "application/pdf":
        return [_file_content_block(str(path))]
    blocks = []
    with pymupdf.open(path) as doc:
        for page in list(doc)[:max_pages]:
            png = page.get_pixmap(dpi=220).tobytes("png")
            b64 = base64.standard_b64encode(png).decode("utf-8")
            blocks.append({"type": "input_image", "image_url": f"data:image/png;base64,{b64}", "detail": "high"})
    return blocks


def _read_twice(path: Path) -> tuple[dict, dict]:
    require(OPENAI_API_KEY, "OPENAI_API_KEY")
    client = OpenAI(api_key=OPENAI_API_KEY)
    return _read(client, [_file_content_block(str(path))]), _read(client, _rendered_blocks(path))


def _date_problem(iso: str | None, created: str | None) -> str | None:
    """Return a description of why the date looks wrong, or None if it's fine."""
    if not iso:
        return "no date read"
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return f"unparseable date {iso!r}"
    if created:
        uploaded = datetime.fromisoformat(created.replace("Z", "+00:00")).date()
        if d > uploaded + timedelta(days=FUTURE_SLACK_DAYS):
            return f"date {iso} is after the upload date {uploaded}"
        if d < uploaded - timedelta(days=MAX_AGE_DAYS):
            return f"date {iso} is more than 2 years before the upload date {uploaded}"
    return None


def analyse(f) -> dict:
    """Download one file, read it twice, and decide a new name or REVIEW."""
    ext = Path(f["name"]).suffix or ".pdf"
    local_path = TMP_DIR / f"{f['id']}{ext}"
    try:
        _download(_service(), f["id"], local_path)
        a, b = _read_twice(local_path)
    finally:
        local_path.unlink(missing_ok=True)

    problems = []
    vendor = canonical_vendor(a["vendor"])
    if vendor != canonical_vendor(b["vendor"]):
        problems.append(f"vendor {vendor!r} vs {canonical_vendor(b['vendor'])!r}")
    if a["invoice_date"] != b["invoice_date"]:
        problems.append(f"date {a['invoice_date']} vs {b['invoice_date']}")
    elif (p := _date_problem(a["invoice_date"], f.get("createdTime"))):
        problems.append(p)
    if _norm(a["invoice_number"]) != _norm(b["invoice_number"]):
        problems.append(f"number {a['invoice_number']!r} vs {b['invoice_number']!r}")

    new_name = f"{vendor}_{a['invoice_date'] or 'no-date'}-{_clean_invnum(a['invoice_number'])}{ext}"
    return {"file": f, "new_name": new_name, "problems": problems}


def rename_folder(folder_id: str, dry_run: bool, limit: int | None = None):
    service = _service()
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    files = list(_walk_files(service, folder_id))
    print(f"Found {len(files)} file(s) total.")
    if limit:
        files = files[:limit]
        print(f"Processing first {len(files)} (--limit {limit}).")
    print()

    def work(f):
        try:
            return analyse(f)
        except Exception as e:  # keep going; report per file
            return {"file": f, "error": str(e)}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(work, files))

    # Files left unchanged (REVIEW / error) keep their names, so new names can't reuse them.
    taken = {r["file"]["name"] for r in results if r.get("error") or r["problems"]}
    review = []
    for r in results:
        f = r["file"]
        if r.get("error"):
            review.append(r)
            print(f"[error]   {f['name']}: {r['error']}")
            continue
        if r["problems"]:
            review.append(r)
            print(f"[REVIEW]  {f['name']}  (would be {r['new_name']})  -- " + "; ".join(r["problems"]))
            continue

        new_name, n = r["new_name"], 1
        while new_name in taken:
            n += 1
            new_name = f"{Path(r['new_name']).stem}_{n}{Path(r['new_name']).suffix}"
        taken.add(new_name)

        action = "[dry-run]" if dry_run else "[renamed]"
        dup = "  (same vendor/date/number as another file -> suffixed)" if n > 1 else ""
        print(f"{action} {f['name']}  ->  {new_name}{dup}")
        if not dry_run and new_name != f["name"]:
            service.files().update(fileId=f["id"], body={"name": new_name}).execute()

    print(f"\n{len(results) - len(review)} handled, {len(review)} need review (left unchanged).")
    for r in review:
        print(f"  review: {r['file']['id']}  {r['file']['name']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/rename_invoices.py <drive_folder_id> [--dry-run] [--limit N]")
        sys.exit(1)

    folder_id = sys.argv[1]
    dry_run = "--dry-run" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    rename_folder(folder_id, dry_run, limit)
