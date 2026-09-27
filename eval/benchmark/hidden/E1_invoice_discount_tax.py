from decimal import Decimal

from invoicekit import Invoice, compute_total


def test_discount_is_taken_before_tax():
    inv = Invoice(discount=Decimal("10.00"), tax_rate=Decimal("0.20")).add("chair", "100.00")
    assert compute_total(inv) == Decimal("108.00")


def test_discount_without_tax():
    inv = Invoice(discount=Decimal("10.00")).add("chair", "100.00")
    assert compute_total(inv) == Decimal("90.00")


def test_discount_tax_and_rounding():
    inv = Invoice(discount=Decimal("9.99"), tax_rate=Decimal("0.075")).add("mug", "33.33", 3)
    assert compute_total(inv) == Decimal("96.75")


def test_no_discount_unchanged():
    inv = Invoice(tax_rate=Decimal("0.10")).add("pen", "2.00", 5)
    assert compute_total(inv) == Decimal("11.00")
