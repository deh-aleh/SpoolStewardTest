"""Comparing a secret that somebody typed (or a browser sent) with the one we hold."""

import hmac


def same_text(given: str, expected: str) -> bool:
    """True when the two texts are equal, in constant time. `hmac.compare_digest` on two `str` refuses any character outside ASCII
    (it raises TypeError: the visitor would get a 500 page instead of a refusal), so the texts are compared as bytes."""
    return hmac.compare_digest(given.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass"))
