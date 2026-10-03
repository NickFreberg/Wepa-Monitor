"""Shared test helpers."""
from __future__ import annotations

import re

import pytest


@pytest.fixture(autouse=True)
def _no_breach_lookups(monkeypatch):
    monkeypatch.setenv("WEPA_BREACH_CHECK", "0")           # tests never call out to Have I Been Pwned


def login(client, username, password, next_="/"):
    """Sign in through the real form (with its CSRF token). Returns the POST response."""
    page = client.get(f"/login?next={next_}").get_data(as_text=True)
    token = re.search(r"name='csrf' value='([^']+)'", page).group(1)
    return client.post(f"/login?next={next_}", data={"username": username, "password": password, "csrf": token})


def csrf_of(client, path):
    return re.search(r"name='csrf' value='([^']+)'", client.get(path).get_data(as_text=True)).group(1)
