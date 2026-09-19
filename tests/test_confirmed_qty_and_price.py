import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from sheets_client import confirmed_qty_and_price


def line(qty, price, amount):
    return {"description": "x", "quantity": qty, "unit_price": price, "amount": amount}


def test_touchtone_two_lines_same_price_collapse_to_88_at_2_25():
    items = [line(44.0, 2.25, 99.0), line(44.0, 2.25, 99.0)]
    assert confirmed_qty_and_price(items, 198.0) == (88, 2.25)


def test_single_line_uses_its_own_qty_and_price():
    assert confirmed_qty_and_price([line(3, 12.5, 37.5)], 37.5) == (3, 12.5)


def test_fractional_quantity_is_kept():
    assert confirmed_qty_and_price([line(2.5, 10.0, 25.0)], 25.0) == (2.5, 10.0)


def test_mixed_prices_stay_blank():
    items = [line(1, 10.0, 10.0), line(1, 20.0, 20.0)]
    assert confirmed_qty_and_price(items, 30.0) == ("", "")


def test_no_line_items_stay_blank():
    assert confirmed_qty_and_price([], 100.0) == ("", "")
    assert confirmed_qty_and_price(None, 100.0) == ("", "")


def test_qty_times_price_not_matching_amount_stays_blank():
    assert confirmed_qty_and_price([line(2, 10.0, 25.0)], 25.0) == ("", "")


def test_amounts_not_matching_subtotal_stay_blank():
    items = [line(44.0, 2.25, 99.0), line(44.0, 2.25, 99.0)]
    assert confirmed_qty_and_price(items, 150.0) == ("", "")


def test_missing_or_zero_quantity_or_price_stays_blank():
    assert confirmed_qty_and_price([{"description": "x", "amount": 10.0}], 10.0) == ("", "")
    assert confirmed_qty_and_price([line(0, 10.0, 0.0)], 0.0) == ("", "")
    assert confirmed_qty_and_price([line(1, None, 10.0)], 10.0) == ("", "")


def test_zero_amount_note_lines_are_ignored():
    # Everything Exterior S17052: one priced line, five $0.00 boilerplate rows.
    items = [line(16.0, 17.85, 285.6)] + [line(1.0, 0.0, 0.0) for _ in range(5)]
    assert confirmed_qty_and_price(items, 285.6) == (16, 17.85)


def test_only_zero_amount_lines_stay_blank():
    assert confirmed_qty_and_price([line(1.0, 0.0, 0.0)], 0.0) == ("", "")
