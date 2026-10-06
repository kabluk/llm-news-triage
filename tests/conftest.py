import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from newstriage.config import load_rules  # noqa: E402


@pytest.fixture(scope="session")
def rules():
    return load_rules(ROOT / "config")


@pytest.fixture
def now():
    return datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)


def make_item(title, url="https://example.org/a", outlet="Example", role="reporting", stype="news",
              snippet="", published="2026-09-29T10:00:00+00:00", event_date=None, extra=None,
              doc_type="news", doc_status="media report", source_id="x"):
    return {"title": title, "url": url, "outlet": outlet, "source_role": role, "source_type": stype,
            "snippet": snippet, "published_at": published, "event_date": event_date, "extra": extra or {},
            "doc_type": doc_type, "doc_status": doc_status, "source_id": source_id, "keys": []}


def make_event(items, **kw):
    ev = {"id": 1, "items": items, "keys": kw.pop("keys", []), "doc_type": kw.pop("doc_type", "news"),
          "doc_status": kw.pop("doc_status", "media report"), "event_date": kw.pop("event_date", None),
          "primary": kw.pop("primary", {}), "novelty_state": kw.pop("novelty_state", "new_event")}
    ev.update(kw)
    return ev


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Tests control the key and model themselves; a real key in the environment must not leak in."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NEWSTRIAGE_MODEL", raising=False)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any attempt to reach a non-local host fails the test."""
    import socket
    real = socket.create_connection

    def guarded(address, *a, **k):
        host = address[0]
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise AssertionError(f"test tried to reach the network: {host}")
        return real(address, *a, **k)
    monkeypatch.setattr(socket, "create_connection", guarded)
