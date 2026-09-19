import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from extract_invoice import _source_name


def test_strips_drive_id_prefix():
    path = "downloads/17ab0WcvDyE_d9oYwyKtVBspfx66ZHNkZ__TouchtoneCanada_2026-04-28-49051.pdf"
    assert _source_name(path) == "TouchtoneCanada_2026-04-28-49051.pdf"


def test_strips_id_containing_underscores_and_dashes():
    path = "downloads/1_dwwqnlmQ46DGoptSPt3T1jR5wFKcooO__Scanned_20260916-1507.pdf"
    assert _source_name(path) == "Scanned_20260916-1507.pdf"


def test_double_underscore_inside_real_name_survives():
    path = "downloads/17ab0WcvDyE_d9oYwyKtVBspfx66ZHNkZ__Vendor__2026-04-28.pdf"
    assert _source_name(path) == "Vendor__2026-04-28.pdf"


def test_gmail_attachment_name_left_alone():
    assert _source_name("downloads/Invoice_1042.pdf") == "Invoice_1042.pdf"


def test_short_prefix_before_double_underscore_is_not_a_drive_id():
    assert _source_name("downloads/my__invoice.pdf") == "my__invoice.pdf"
