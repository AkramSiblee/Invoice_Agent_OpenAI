import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_new_invoices import plan_log_updates


def row(n, file_name):
    return {"_row_number": n, "file_name": file_name}


def test_single_matching_row_is_updated():
    rows = [row(2, "a.pdf"), row(3, "1789861471279blob.jpg")]
    updates, warnings = plan_log_updates(rows, [("1789861471279blob.jpg", "Vendor_2026-09-17-123.jpg")])
    assert updates == [(3, "Vendor_2026-09-17-123.jpg")]
    assert warnings == []


def test_no_matching_row_warns_instead_of_guessing():
    updates, warnings = plan_log_updates([row(2, "a.pdf")], [("blob.jpg", "New.jpg")])
    assert updates == []
    assert len(warnings) == 1 and "0 row(s)" in warnings[0]


def test_several_matching_rows_warn_and_change_nothing():
    rows = [row(2, "blob.jpg"), row(3, "blob.jpg")]
    updates, warnings = plan_log_updates(rows, [("blob.jpg", "New.jpg")])
    assert updates == []
    assert len(warnings) == 1 and "2 row(s)" in warnings[0]


def test_several_renames_are_planned_independently():
    rows = [row(2, "a.jpg"), row(3, "b.jpg")]
    updates, warnings = plan_log_updates(rows, [("a.jpg", "A.jpg"), ("b.jpg", "B.jpg")])
    assert updates == [(2, "A.jpg"), (3, "B.jpg")]
    assert warnings == []
