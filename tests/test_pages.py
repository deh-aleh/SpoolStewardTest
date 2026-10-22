"""Counting the pages of a document before it is printed (spoolsteward/policy/pages.py)."""

import io

import pytest
from pypdf import PdfWriter

from spoolsteward.policy.pages import count_pages


def pdf(pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(595, 842)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def test_a_pdf_is_counted_by_its_real_pages():
    assert count_pages(pdf(1), "application/pdf") == 1
    assert count_pages(pdf(37), "application/pdf") == 37


@pytest.mark.parametrize("declared", [1, 2, 9])
def test_a_pdf_that_lies_about_its_page_count_is_counted_by_its_real_pages(declared):
    """The /Count of the page tree is only a claim. A file that says 1 and holds 5 (poppler's pdfinfo believes it) must count 5, or
    the pages limit could be passed by a crafted file. This pins the behaviour of the pypdf version in requirements/runtime.txt: a
    newer pypdf that trusted /Count would fail here, and be noticed."""
    data = pdf(5)
    assert b"/Count 5" in data
    forged = data.replace(b"/Count 5", b"/Count %d" % declared)       # one digit for one digit: no offset of the file moves
    assert forged != data
    assert count_pages(forged, "application/pdf") == 5


def test_a_damaged_or_encrypted_pdf_is_left_to_cups():
    assert count_pages(b"%PDF-1.7\n garbage", "application/pdf") is None
    w = PdfWriter()
    w.add_blank_page(100, 100)
    w.encrypt("x")
    out = io.BytesIO()
    w.write(out)
    assert count_pages(out.getvalue(), "application/pdf") is None
