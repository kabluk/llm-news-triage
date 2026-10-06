"""URL, date and text normalization."""
from __future__ import annotations

import html
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = re.compile(
    r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|cmpid$|CMP$|ref$|ref_src$|smid$|"
    r"taid$|mod$|guccounter$|ocid$|__twitter_impression$|at_medium$|at_campaign$)"
)


def canonical_url(url: str) -> str:
    """Canonical URL for deduplication.

    Lowercase scheme and host, no www./m., no fragment, no tracking params,
    no trailing slash, no /amp suffix, sorted query."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") and host.count(".") >= 2:
        host = host[2:]
    path = re.sub(r"/+", "/", parts.path or "/")
    path = re.sub(r"/amp/?$", "", path)
    if len(path) > 1:
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if not TRACKING_PARAMS.match(k)]
    query.sort()
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_dt(value) -> datetime | None:
    """Parses RFC 822, ISO 8601, 'YYYY-MM-DD', unix time, time.struct_time."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if hasattr(value, "tm_year"):
        return datetime(*value[:6], tzinfo=timezone.utc)
    s = str(value).strip()
    if re.fullmatch(r"\d{9,11}", s):
        return datetime.fromtimestamp(int(s), tz=timezone.utc)
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in ("%A, %B %d, %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def snippet(s: str | None, limit: int = 300) -> str:
    """Short excerpt. Full text of third-party articles is never stored."""
    s = strip_html(s)
    s = re.sub(r"\s*(See more\.\.\.|Continue reading\.*|Read more\.*)\s*$", "", s, flags=re.I)
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0]
    return cut + "…"


STOP = set("""a an the and or of to in on for at by with from as is are was were be been
has have had it its this that these those after before over under into about amid
new says said say will would could can may might not no than then who what why how
us u.s just more most out up down off via per vs v our your their we you""".split())


def tokens(text: str) -> set[str]:
    """Significant title words used to compare events."""
    text = (text or "").lower()
    text = re.sub(r"[’']s\b", "", text)
    words = re.findall(r"[a-z0-9][a-z0-9\-\.]*[a-z0-9]|[a-z0-9]", text)
    out = set()
    for w in words:
        w = w.strip(".-")
        if len(w) < 3 or w in STOP:
            continue
        if w.endswith("s") and len(w) > 4 and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def any_match(patterns, text: str) -> list[str]:
    hits = []
    for p in patterns or []:
        if re.search(p, text or "", flags=re.I):
            hits.append(p)
    return hits


def anthropic_key() -> str | None:
    """Claude API key: read from ANTHROPIC_API_KEY only."""
    return os.environ.get("ANTHROPIC_API_KEY") or None
