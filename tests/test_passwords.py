"""Local account passwords: the cost of a check, and how many of them may run at the same time."""

import asyncio
import threading
import time

import pytest
from argon2 import PasswordHasher

from spoolsteward.auth import passwords
from spoolsteward.auth.passwords import check_password, hash_password, verify_password
from spoolsteward.config import Settings, settings

GOOD = "correct horse battery"


def test_new_hashes_cost_32_mib_three_passes_one_lane():
    assert hash_password(GOOD).startswith("$argon2id$v=19$m=32768,t=3,p=1$")


def test_hashes_made_with_the_old_cost_still_verify():
    old = PasswordHasher().hash(GOOD)                    # the library default: 64 MiB, four lanes
    assert "m=65536,t=3,p=4" in old
    assert verify_password(old, GOOD) and not verify_password(old, GOOD + "x")


def test_the_default_number_of_checks_at_once_is_three():
    assert Settings(_env_file=None).password_checks_at_once == 3


@pytest.fixture
def counting(monkeypatch):
    """verify_password replaced by a slow function that remembers how many ran at the same moment."""
    seen = {"now": 0, "most": 0}
    lock = threading.Lock()

    def slow(stored_hash, password):
        with lock:
            seen["now"] += 1
            seen["most"] = max(seen["most"], seen["now"])
        time.sleep(0.15)
        with lock:
            seen["now"] -= 1
        return password == "ok"
    monkeypatch.setattr(passwords, "verify_password", slow)
    return seen


@pytest.mark.parametrize("limit", [1, 3])
async def test_only_that_many_checks_run_at_once_and_all_of_them_finish(counting, monkeypatch, limit):
    monkeypatch.setattr(settings, "password_checks_at_once", limit)
    results = await asyncio.gather(*[check_password("h", "ok" if i % 2 else "no") for i in range(12)])
    assert results == [bool(i % 2) for i in range(12)]            # each caller gets its own answer
    assert counting["most"] == limit


async def test_the_limit_is_read_again_when_the_setting_changes(counting, monkeypatch):
    monkeypatch.setattr(settings, "password_checks_at_once", 1)
    await asyncio.gather(*[check_password("h", "ok") for _ in range(4)])
    assert counting["most"] == 1
    counting["most"] = 0
    monkeypatch.setattr(settings, "password_checks_at_once", 4)
    await asyncio.gather(*[check_password("h", "ok") for _ in range(4)])
    assert counting["most"] == 4


async def test_a_real_check_through_the_gate():
    h = hash_password(GOOD)
    assert await check_password(h, GOOD) is True
    assert await check_password(h, GOOD + "x") is False
    assert await check_password(None, GOOD) is False             # unknown account: false, after the same work
