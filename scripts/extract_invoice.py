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
- subtotal, tax, total: numbers. The true `total` is the amount on the line literally labeled TOTAL (or, if genuinely absent, the highest subtotal-like figure before any payment breakdown begins). Below that TOTAL line, receipts often print a payment breakdown — one line per tender (VISA, MASTERCARD, DEBIT, INTERAC, CASH, CHEQUING, etc.), each usually next to a masked card number (e.g. "XXXXXXXXXXXX1234") or an AUTH CODE. Amounts on those tender lines are portions of how the total was paid, NOT the total itself — a split payment (e.g. part VISA + remaining balance on DEBIT) means no single tender line equals the total, and even a single-tender payment shouldn't be used to read the total when an explicit TOTAL line exists above it. Never take a number from a line containing a masked card number, AUTH CODE, or a payment-method word as subtotal, tax, or total.

  Some receipts (fuel pumps especially) price tax-INCLUSIVE: the "Sub Total"/"TOTAL" lines already have tax baked into the unit price, so the receipt's own subtotal-labeled line equals its total, and any "tax on" line shows $0.00 — but the receipt separately discloses the embedded tax elsewhere, e.g. "Fuel Includes GST 5.0% $4.29". When you see a disclosure like that, don't report tax as $0: compute subtotal = total - disclosed_tax, and tax = the disclosed amount, so subtotal + tax still equals total. Only do this when the document explicitly discloses the embedded tax amount — never estimate or back-calculate a tax that isn't printed anywhere on the document.

  When you apply that adjustment, also carry it into `line_items`: each affected line's `amount` (and `unit_price`, if quantity is 1) must be its tax-EXCLUSIVE share, not the tax-inclusive figure printed on the receipt — so `line_items` amounts still sum to `subtotal`. E.g. a pump line printed as "Fuel $100.00" with disclosed "Includes GST 5.0% $4.76" should be reported as amount $95.24, not $100.00.
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

If the scan/photo is faint, blurry, or has overlapping/garbled text, still read every visible line item — don't silently drop a line just because its description is hard to make out. If a line's amount is legible but its description isn't, use a description like "(item illegible in scan)" rather than omitting the line or inventing wording that isn't really there. When reconstructing subtotal/tax/total, prefer the document's own printed SUBTOTAL/TAX/TOTAL figures over summing line items you're unsure you read correctly — a low-confidence per-line reading is more likely to be wrong than the printed summary figures.
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
