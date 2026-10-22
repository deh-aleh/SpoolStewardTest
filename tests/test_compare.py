"""spoolsteward.auth.compare.same_text: equal texts are equal, anything else is just "no" (never an error)."""

import pytest

from spoolsteward.auth.compare import same_text


@pytest.mark.parametrize("a, b, expected", [
    ("abc", "abc", True), ("", "", True), ("abc", "abd", False), ("abc", "abcd", False), ("", "x", False),
    ("café", "café", True), ("café", "cafe", False), ("日本語", "日本語", True), ("日本語", "abc", False), ("😀", "😀", True),
    ("\ud800", "\ud800", True), ("\ud800", "x", False),          # a lone surrogate is still only a text
])
def test_same_text(a, b, expected):
    assert same_text(a, b) is expected
    assert same_text(b, a) is expected


def test_plain_compare_digest_would_have_raised():
    import hmac

    with pytest.raises(TypeError):
        hmac.compare_digest("abc", "café")
