"""Pins _parse_permissions (the permission fields of the user and group forms) branch by branch."""

import pytest

from app.admin.accounts import _parse_permissions
from app.policy import permissions

pytestmark = pytest.mark.usefixtures("english")

NONE = {"color": None, "one_sided": None, "printers_mode": None, "printers": None,
        "hours_mode": None, "hours_days": None, "hours_start": None, "hours_end": None}


def user(**kw):
    a = dict(color="", one_sided="", printers_mode="", printers=[], hours_mode="", days=[], start="", end="")
    a.update(kw)
    return _parse_permissions(True, a["color"], a["one_sided"], a["printers_mode"], a["printers"], a["hours_mode"], a["days"], a["start"], a["end"])


def group(**kw):
    a = dict(color="", one_sided="", printers_mode="all", printers=[], hours_mode="any", days=[], start="", end="")
    a.update(kw)
    return _parse_permissions(False, a["color"], a["one_sided"], a["printers_mode"], a["printers"], a["hours_mode"], a["days"], a["start"], a["end"])


def test_a_user_form_with_nothing_chosen_stores_nothing():
    assert user() == NONE


def test_user_flags_are_three_state_and_trimmed():
    out = user(color=" 1 ", one_sided="0")
    assert out["color"] is True and out["one_sided"] is False
    assert user(color="")["color"] is None


def test_group_flags_store_only_the_restrictions():
    out = group(color="1", one_sided="")
    assert out["color"] is None and out["one_sided"] is False
    assert group(color="0")["color"] is False


@pytest.mark.parametrize("kw", [dict(color="2"), dict(one_sided="yes"), dict(printers_mode="x"), dict(hours_mode="x")])
def test_invalid_choices(kw):
    with pytest.raises(ValueError, match="^Invalid permission value$"):
        user(**kw)


def test_the_flags_are_checked_before_the_printers_and_the_hours():
    with pytest.raises(ValueError, match="Invalid permission value"):
        user(color="9", printers_mode="list", printers=["a b"])


def test_a_group_must_choose_a_printer_mode_and_an_hours_mode():
    with pytest.raises(ValueError, match="^Invalid permission value$"):
        group(printers_mode="")
    with pytest.raises(ValueError, match="^Invalid permission value$"):
        group(hours_mode="")


def test_a_group_that_allows_everything_stores_nothing():
    assert group(color="1", one_sided="1") == NONE


def test_the_printer_list_is_normalised():
    out = user(printers_mode="list", printers=[" Color ", "bw", "COLOR"])
    assert out["printers_mode"] == "list" and out["printers"] == ["bw", "color"]
    assert group(printers_mode="list", printers=["bw"])["printers"] == ["bw"]
    assert user(printers_mode="all")["printers_mode"] == "all" and user(printers_mode="all")["printers"] is None
    assert user(printers_mode="", printers=["x y"])["printers"] is None


def test_printer_list_errors():
    with pytest.raises(ValueError, match="^Invalid printer name 'a b'$"):
        user(printers_mode="list", printers=["ok", "a b"])
    with pytest.raises(ValueError, match="^Invalid printer name '" + "x" * 40 + "'$"):
        user(printers_mode="list", printers=["x" * 40 + "!"])
    with pytest.raises(ValueError, match="^Choose at least one printer, or choose “all printers”$"):
        user(printers_mode="list", printers=[])
    n = permissions.MAX_PRINTERS + 1
    with pytest.raises(ValueError, match=f"^Too many printers in the list \\(at most {permissions.MAX_PRINTERS}\\)$"):
        user(printers_mode="list", printers=[f"p{i}" for i in range(n)])
    assert len(user(printers_mode="list", printers=[f"p{i}" for i in range(permissions.MAX_PRINTERS)])["printers"]) == permissions.MAX_PRINTERS
    assert len(user(printers_mode="list", printers=["p1"] * n)["printers"]) == 1       # counted after the duplicates are dropped


def test_the_hours_window():
    out = user(hours_mode="window", days=["3", "1", "1"], start="08:00", end="17:30")
    assert out["hours_mode"] == "window" and out["hours_days"] == "13"
    assert (out["hours_start"], out["hours_end"]) == (permissions.parse_time("08:00"), permissions.parse_time("17:30"))
    assert user(hours_mode="any")["hours_mode"] == "any" and user(hours_mode="any")["hours_days"] is None
    assert group(hours_mode="window", days=["0"], start="22:00", end="06:00")["hours_mode"] == "window"


@pytest.mark.parametrize("kw, msg", [
    (dict(days=[], start="08:00", end="09:00"), "^Choose at least one day of the week for the hours$"),
    (dict(days=["7"], start="08:00", end="09:00"), "^Choose at least one day of the week for the hours$"),
    (dict(days=["0"], start="8", end="09:00"), "^Enter the hours as HH:MM, for example 08:00 and 17:30$"),
    (dict(days=["0"], start="08:00", end="24:01"), "^Enter the hours as HH:MM, for example 08:00 and 17:30$"),
    (dict(days=["0"], start="08:00", end="08:00"), "^The start and the end of the hours must be different$"),
])
def test_hours_window_errors(kw, msg):
    with pytest.raises(ValueError, match=msg):
        user(hours_mode="window", **kw)


def test_printer_errors_come_before_hours_errors():
    with pytest.raises(ValueError, match="Choose at least one printer"):
        user(printers_mode="list", hours_mode="window")
