"""Web search for the short-fact route (docs/DESIGN.md, "Search").

When the two models disagree on a short fact, neither is sure, or the fact can
change over time, a third model reads what a search found and must quote the
words that settle it. A source counts only if this server loaded the page
itself and the quote is on it: a link never appears because a model wrote one.

  sources   Wikipedia (free, no key, on unless switched off) and, once its key
            is set, Tavily (a web search API whose free plan has 1,000 searches
            a month, so it is capped per day here)
  loading   https only, to public addresses only (checked again at every
            redirect, on top of the pod's network policy); HTML or plain text;
            at most fetch_timeout_s and max_page_bytes per page
  evidence  a page's main text, without scripts, menus or citation marks [12];
            the passages sharing most words with the question and the candidate
            answers go to the model, as untrusted data it is told not to obey
"""
from __future__ import annotations

import concurrent.futures as cf
import ipaddress
import logging
import re
import socket
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

import httpx

from . import telemetry as tm
from .config import VERSION, cfg, registry
from .secretstore import SecretNotFound, get_secret

log = logging.getLogger("crossfact.search")

DEFAULTS = {
    "enabled": True,
    "wikipedia": {"enabled": True, "lang": "en", "pages": 3},
    "tavily": {"secret": "tavily_api_key", "results": 5, "daily_limit": 30},
    "pages": 5,                       # pages loaded per search, at most
    "fetch_timeout_s": 8,
    "max_page_bytes": 3_000_000,
    "exclude_domains": ["huggingface.co", "kaggle.com"],   # copies of benchmark answers
}
PAGE_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
MAX_REDIRECTS = 3


@dataclass
class Source:
    url: str
    title: str
    provider: str            # "wikipedia" | "tavily"
    snippet: str = ""        # what the search service said about it
    text: str = ""           # the page's main text, as loaded here ("" if it couldn't be)
    error: str = ""          # why it couldn't be loaded
    extra: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"url": self.url, "title": self.title, "provider": self.provider,
                "loaded": bool(self.text), "error": self.error or None}


class SearchError(RuntimeError):
    pass


def setting(key: str, default=None):
    merged = {**DEFAULTS, **(registry().get("search") or {})}
    return merged.get(key, default)


def _provider_settings(name: str) -> dict:
    return {**DEFAULTS[name], **(setting(name) or {})}


def _tavily_key() -> str | None:
    try:
        return get_secret(_provider_settings("tavily")["secret"])
    except SecretNotFound:
        return None


def providers() -> list[str]:
    """The search services this server can use right now."""
    if not setting("enabled"):
        return []
    out = []
    if _provider_settings("wikipedia").get("enabled", True):
        out.append("wikipedia")
    if _provider_settings("tavily").get("enabled", True) and _tavily_key():
        out.append("tavily")
    return out


def available() -> bool:
    return bool(providers())


# ---- the search services ------------------------------------------------------------------

def _user_agent() -> str:
    """Wikimedia asks for a User-Agent that says who is calling and how to reach them."""
    site = cfg().get("base_url") or "self-hosted"
    return f"Crossfact/{VERSION[:12]} (+{site}; checks quotes for a question-answering app)"


def _wikipedia(query: str, n: int) -> list[Source]:
    lang = re.sub(r"[^a-z-]", "", str(_provider_settings("wikipedia").get("lang", "en"))) or "en"
    resp = HTTP.get(f"https://{lang}.wikipedia.org/w/api.php",
                    params={"action": "query", "list": "search", "srsearch": query, "srlimit": n,
                            "format": "json", "utf8": 1},
                    headers={"User-Agent": _user_agent()}, timeout=float(setting("fetch_timeout_s")))
    if resp.status_code != 200:
        raise SearchError(f"wikipedia {resp.status_code}")
    hits = (resp.json().get("query") or {}).get("search") or []
    return [Source(url=f"https://{lang}.wikipedia.org/wiki/{quote(h['title'].replace(' ', '_'))}",
                   title=f"{h['title']} (Wikipedia)", provider="wikipedia",
                   snippet=re.sub(r"<[^>]+>", "", h.get("snippet", "")))
            for h in hits if h.get("title")]


_tavily_lock = threading.Lock()
_tavily_used: dict[str, int] = {}   # UTC day -> searches made


def _tavily(query: str, n: int) -> list[Source]:
    conf = _provider_settings("tavily")
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with _tavily_lock:
        if day not in _tavily_used:  # a new UTC day: start counting again
            _tavily_used.clear()
            _tavily_used[day] = 0
        if _tavily_used[day] >= int(conf["daily_limit"]):
            raise SearchError("tavily daily limit reached")
        _tavily_used[day] += 1
    key = _tavily_key() or ""
    resp = HTTP.post("https://api.tavily.com/search",
                     json={"api_key": key, "query": query, "search_depth": "basic", "max_results": n,
                           "exclude_domains": list(setting("exclude_domains") or [])},
                     headers={"Authorization": f"Bearer {key}"}, timeout=float(setting("fetch_timeout_s")))
    if resp.status_code != 200:
        raise SearchError(f"tavily {resp.status_code}")
    return [Source(url=r["url"], title=r.get("title") or r["url"], provider="tavily", snippet=r.get("content") or "")
            for r in resp.json().get("results") or [] if str(r.get("url", "")).startswith("https://")]


SERVICES = {"wikipedia": _wikipedia, "tavily": _tavily}


def _excluded(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in setting("exclude_domains") or [])


def search(query: str) -> tuple[list[Source], list[str]]:
    """Ask every available service, then load the pages they found (in parallel).
    Returns (sources, errors): the sources in order, each with its page text or
    the reason it couldn't be loaded."""
    found, errors, seen = [], [], set()
    for name in providers():
        n = int(_provider_settings(name).get("pages" if name == "wikipedia" else "results", 3))
        try:
            hits = SERVICES[name](query, n)
            tm.SEARCHES.labels(name, "ok" if hits else "empty").inc()
        except (SearchError, httpx.HTTPError, ValueError, KeyError) as e:
            tm.SEARCHES.labels(name, "capped" if "limit" in str(e) else "error").inc()
            errors.append(f"{name}: {e}"[:200])
            continue
        for s in hits:
            if s.url not in seen and not _excluded(s.url):
                seen.add(s.url)
                found.append(s)
    found = found[:int(setting("pages"))]
    if found:
        with cf.ThreadPoolExecutor(max_workers=min(5, len(found)), thread_name_prefix="fetch") as ex:
            for s, (text, error) in zip(found, ex.map(lambda src: fetch_text(src.url), found), strict=True):
                s.text, s.error = text, error
    return found, errors


# ---- loading a page, safely ---------------------------------------------------------------

def _public_host(host: str) -> bool:
    """Every address the name resolves to is a public one (no private, loopback,
    link-local or metadata addresses: 169.254.169.254 is the cloud's)."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return False
    addrs = {info[4][0] for info in infos}
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


def _allowed(url: str) -> str:
    """'' if the URL may be loaded, else why not."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return "not an https URL"
    if parts.port not in (None, 443):
        return "not the https port"
    if parts.username or parts.password:
        return "has credentials in it"
    if not _public_host(parts.hostname):
        return "not a public address"
    return ""


def fetch_text(url: str) -> tuple[str, str]:
    """(page text, '') or ('', why it couldn't be loaded)."""
    limit = int(setting("max_page_bytes"))
    for _ in range(MAX_REDIRECTS + 1):
        why = _allowed(url)
        if why:
            tm.PAGE_FETCHES.labels("blocked").inc()
            return "", why
        try:
            with HTTP.stream("GET", url, headers={"User-Agent": _user_agent(), "Accept": "text/html,text/plain"},
                             timeout=float(setting("fetch_timeout_s"))) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    url = urljoin(url, resp.headers["location"])
                    continue
                if resp.status_code != 200:
                    tm.PAGE_FETCHES.labels("error").inc()
                    return "", f"the page answered {resp.status_code}"
                kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind not in PAGE_TYPES:
                    tm.PAGE_FETCHES.labels("not_page").inc()
                    return "", f"not a web page ({kind or 'no type'})"
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > limit:
                        tm.PAGE_FETCHES.labels("too_big").inc()
                        return "", "the page is too big"
                raw = body.decode(resp.encoding or "utf-8", errors="replace")
        except httpx.HTTPError as e:
            tm.PAGE_FETCHES.labels("error").inc()
            return "", f"couldn't load it ({type(e).__name__})"
        text = page_text(raw) if kind != "text/plain" else clean(raw)
        tm.PAGE_FETCHES.labels("ok" if text else "error").inc()
        return (text, "") if text else ("", "no text on the page")
    tm.PAGE_FETCHES.labels("error").inc()
    return "", "too many redirects"


class _Text(HTMLParser):
    """A page's readable text: no scripts, styles, menus, forms or citation marks;
    a line break at every block; only the main content when the page marks it
    (<main>, or Wikipedia's #mw-content-text)."""

    SKIP = {"script", "style", "noscript", "svg", "template", "head", "nav", "form", "button", "select", "footer"}
    SKIP_CLASSES = {"reference", "mw-editsection", "navbox", "mw-jump-link"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article",
             "blockquote", "dd", "dt", "caption", "pre", "hr", "ul", "ol"}
    MAIN_IDS = {"mw-content-text", "main-content", "content-main"}
    VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.all: list[str] = []
        self.main: list[str] = []
        self.open: list[tuple[str, bool, bool]] = []   # (tag, skipped, opened the main region)
        self.skip = 0
        self.in_main = 0
        self.found_main = False

    def handle_starttag(self, tag, attrs):
        if tag in self.VOID:
            if tag in ("br", "hr"):
                self._put("\n")
            return
        a = dict(attrs)
        main = tag == "main" or a.get("id") in self.MAIN_IDS
        skipped = tag in self.SKIP or bool(self.SKIP_CLASSES & set((a.get("class") or "").split()))
        self.open.append((tag, skipped, main))
        self.skip += skipped
        self.in_main += main
        self.found_main |= main
        if tag in self.BLOCK:
            self._put("\n")
        elif tag in ("td", "th"):
            self._put(" ")

    def handle_endtag(self, tag):
        # close up to the matching tag: pages in the wild don't always close what they open
        for i in range(len(self.open) - 1, -1, -1):
            if self.open[i][0] == tag:
                for _, skipped, main in self.open[i:]:
                    self.skip -= skipped
                    self.in_main -= main
                del self.open[i:]
                break
        if tag in self.BLOCK:
            self._put("\n")

    def handle_data(self, data):
        if not self.skip:
            self._put(data)

    def _put(self, s: str):
        self.all.append(s)
        if self.in_main:
            self.main.append(s)


_CITE = re.compile(r"\[(?:\d+|[a-z]|note \d+|citation needed|edit|update|clarification needed)\]", re.I)


def clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _CITE.sub("", text)
    lines = (re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n"))
    return "\n".join(ln for ln in lines if ln)


def page_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:  # a broken page is just a page with less text
        log.debug("html parse stopped early", exc_info=True)
    return clean("".join(p.main if p.found_main and p.main else p.all))


# ---- evidence and quotes ------------------------------------------------------------------

_STOP = set("""a an and are as at be been by did do does for from had has have how i in is it its many much of on or
that the their there this to was were what when where which who whom whose why will with you your
current currently latest today now""".split())
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "−": "-", " ": " "})


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").translate(_QUOTES).casefold()
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip()


def terms(*texts: str) -> set[str]:
    return {w for w in re.findall(r"[0-9]+(?:\.[0-9]+)*|[a-z]+", norm(" ".join(texts))) if w not in _STOP}


def passages(text: str, words: set[str], budget: int = 2500) -> str:
    """The lines and sentences of a page that share the most words with the question
    and the answers, in page order, up to about `budget` characters."""
    chunks = []
    for line in text.split("\n"):
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", line) if len(line) > 400 else [line]
        chunks += [p.strip() for p in parts if 12 <= len(p.strip()) <= 700]
    scored = sorted(((len(words & terms(c)), -i) for i, c in enumerate(chunks)), reverse=True)
    picked, used = [], 0
    for score, neg_i in scored:
        if score == 0 or used >= budget:
            break
        picked.append(-neg_i)
        used += len(chunks[-neg_i])
    return "\n".join(chunks[i] for i in sorted(picked))


def quoted(quote: str, text: str) -> bool:
    """The quote is on the page: word for word, ignoring case, spacing, curly quotes
    and accents; "..." may stand for words left out between its parts."""
    q = norm(quote).strip(" .\"'")
    if len(q) < 8:
        return False
    page = norm(text)
    pos = 0
    for part in [p.strip(" .\"'") for p in re.split(r"\.\.\.|…", q)]:
        if len(part) < 4:
            continue
        found = page.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    return pos > 0


def states(answer: str, quote: str) -> bool:
    """The quote states the answer: every number in the answer is in it (3.15 in
    "3.15.0") and, for an answer without numbers, every word."""
    q = norm(quote)
    nums = re.findall(r"\d+(?:[.,]\d+)*", norm(answer))
    if nums:
        return all(re.search(rf"(?<![\d.,]){re.escape(n.replace(',', ''))}(?![\d])", q.replace(",", ""))
                   for n in nums)
    words = terms(answer)
    return bool(words) and all(re.search(rf"\b{re.escape(w)}\b", q) for w in words)


class _Client:
    """The HTTP calls this module makes, in one place (tests swap it for a fake)."""

    def get(self, url, **kw):
        return httpx.get(url, **kw)

    def post(self, url, **kw):
        return httpx.post(url, **kw)

    def stream(self, method, url, **kw):
        return httpx.stream(method, url, **kw)


HTTP = _Client()
