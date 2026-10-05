"""Polite HTTP client: own User-Agent, timeouts, per-host crawl delay, optional
robots.txt check, and detection of blocks and CAPTCHA pages. Blocks are never
circumvented: they become a source error in the log."""
from __future__ import annotations

import time
import urllib.robotparser
from urllib.parse import urlsplit

import requests


class FetchError(Exception):
    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind          # blocked | captcha | robots | http | network | parse | config | rate_limited
        self.status = status


CHALLENGE_MARKERS = (
    "i am not a robot", "captcha", "are you a robot", "verify you are human",
    "access denied", "request blocked", "attention required", "just a moment...",
)


class Http:
    def __init__(self, user_agent: str, timeout: int = 30, retries: int = 2,
                 default_delay: float = 2.0):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = user_agent
        self.timeout = timeout
        self.retries = retries
        self.default_delay = default_delay
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _wait(self, host: str, delay: float | None):
        d = self.default_delay if delay is None else delay
        last = self._last.get(host)
        if last is not None:
            gap = time.monotonic() - last
            if gap < d:
                time.sleep(d - gap)
        self._last[host] = time.monotonic()

    def robots_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=self.timeout)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots.txt: the page status decides
            except requests.RequestException:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.s.headers["User-Agent"], url)

    def get(self, url: str, *, params=None, headers=None, delay: float | None = None,
            check_robots: bool = False, expect: str | None = None,
            timeout: float | None = None) -> requests.Response:
        if check_robots and not self.robots_allowed(url):
            raise FetchError("robots", f"robots.txt disallows {url}")
        host = urlsplit(url).netloc
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait(host, delay)
            try:
                r = self.s.get(url, params=params, headers=headers, timeout=timeout or self.timeout)
            except requests.RequestException as e:
                last_exc = FetchError("network", f"{type(e).__name__}: {e}")
                time.sleep(2 * (attempt + 1))
                continue
            head = r.text[:4000].lower() if "text" in r.headers.get("content-type", "") else ""
            if r.status_code in (401, 403, 429) or (expect == "json" and "json" not in r.headers.get("content-type", "")):
                if any(m in head for m in CHALLENGE_MARKERS[:4]):
                    raise FetchError("captcha", f"{r.status_code}: CAPTCHA page, not bypassed", r.status_code)
                if r.status_code == 429:
                    ra = r.headers.get("Retry-After", "")
                    if ra.isdigit() and int(ra) > 60:
                        raise FetchError("rate_limited", f"429: rate limited, retry in {int(ra) // 60} min", 429)
                    if attempt < self.retries:
                        time.sleep(min(int(ra), 60) if ra.isdigit() else 10 * (attempt + 1))
                        last_exc = FetchError("rate_limited", "429: rate limited", 429)
                        continue
                    raise FetchError("rate_limited", "429: rate limited (retries exhausted)", 429)
                if r.status_code in (401, 403):
                    msg = "access denied" if "access denied" in head else "forbidden"
                    raise FetchError("blocked", f"{r.status_code}: {msg}", r.status_code)
                if expect == "json":
                    snippet = r.text[:200].replace("\n", " ")
                    raise FetchError("parse", f"expected JSON, got {r.headers.get('content-type')}: {snippet}")
            if r.status_code >= 500:
                last_exc = FetchError("http", f"HTTP {r.status_code}", r.status_code)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise FetchError("http", f"HTTP {r.status_code}", r.status_code)
            return r
        raise last_exc or FetchError("network", "unknown error")
