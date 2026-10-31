"""Search for short facts: what a page's text is, what counts as a quote, what may be
loaded at all, and the route end to end, against a fake web."""
import json
import socket
from contextlib import contextmanager

import pytest

from tests.test_pipeline import A, B, stages

PYTHON_PAGE = """<html><head><title>Python - Wikipedia</title><script>var x = 1;</script></head><body>
<nav>Main menu Contents Current events</nav><a class="mw-jump-link" href="#c">Jump to content</a>
<div id="mw-content-text"><table class="infobox">
<tr><th>Paradigm</th><td>Multi-paradigm</td></tr>
<tr><th>Stable release</th><td>3.15.0<sup class="reference">[2]</sup>&nbsp;/ 9&nbsp;October 2026</td></tr></table>
<p><b>Python</b> is a high-level, general-purpose programming language.<sup class="reference">[12]</sup></p>
<h2>History<span class="mw-editsection">[<a>edit</a>]</span></h2>
<p>Python 3.14 was released on 7 October 2025, and Python 3.15 followed on 9 October 2026.</p>
</div><footer>Privacy policy</footer></body></html>"""


class Resp:
    def __init__(self, status=200, body=b"", headers=None, data=None):
        self.status_code, self.body, self.data = status, body, data
        self.headers = {"content-type": "text/html; charset=utf-8", **(headers or {})}
        self.encoding = "utf-8"

    def json(self):
        return self.data

    def iter_bytes(self):
        for i in range(0, len(self.body), 4096):
            yield self.body[i:i + 4096]


class FakeWeb:
    """Wikipedia's search API, Tavily, and some pages; every request is logged."""

    def __init__(self):
        self.pages: dict[str, Resp] = {}
        self.wiki: list[str] = []
        self.tavily: list[dict] = []
        self.log: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.log.append(f"GET {url} {(params or {}).get('srsearch', '')}")
        assert "Crossfact/" in headers["User-Agent"]   # Wikimedia asks callers to say who they are
        return Resp(data={"query": {"search": [{"title": t, "snippet": f"about <b>{t}</b>"} for t in self.wiki]}})

    def post(self, url, json=None, headers=None, timeout=None):
        self.log.append(f"POST {url} {json['query']}")
        return Resp(data={"results": self.tavily})

    @contextmanager
    def stream(self, method, url, headers=None, timeout=None):
        self.log.append(f"FETCH {url}")
        yield self.pages.get(url, Resp(status=404))


@pytest.fixture
def web(llm, monkeypatch):
    from app import search
    from app.config import registry

    fake = FakeWeb()
    monkeypatch.setattr(search, "HTTP", fake)
    monkeypatch.setattr(search, "_public_host", lambda host: True)
    monkeypatch.setitem(registry(), "search", {"enabled": True, "wikipedia": {"pages": 2}})
    return fake


WIKI = "https://en.wikipedia.org/wiki/"


# ---- page text, quotes ----------------------------------------------------------------------

def test_a_pages_text_is_its_main_content_without_menus_scripts_or_citation_marks():
    from app.search import page_text

    text = page_text(PYTHON_PAGE)
    assert text.splitlines()[:2] == ["Paradigm Multi-paradigm", "Stable release 3.15.0 / 9 October 2026"]
    assert "Main menu" not in text and "var x" not in text and "[12]" not in text and "[edit]" not in text
    assert "Privacy policy" not in text


def test_a_quote_must_be_on_the_page_and_state_the_answer():
    from app.search import page_text, quoted, states

    text = page_text(PYTHON_PAGE)
    assert quoted("Stable release 3.15.0 / 9 October 2026", text)
    assert quoted("“Python 3.14 was released … Python 3.15 followed on 9 October 2026”", text)  # curly, elided
    assert not quoted("Python 3.16 was released", text) and not quoted("3.15", text)       # made up; too short
    assert states("3.15", "Stable release 3.15.0") and states("Python 3.15", "Stable release 3.15.0")
    assert not states("3.1", "Stable release 3.15.0")
    assert states("António Guterres", "Antonio Guterres is the ninth Secretary-General")
    assert not states("Marie Curie", "Curie won it twice")                                   # every word, for names


def test_the_passages_shown_are_the_ones_about_the_question():
    from app.search import page_text, passages, terms

    shown = passages(page_text(PYTHON_PAGE), terms("What is the latest stable version of Python?"))
    assert "Stable release 3.15.0" in shown and "Multi-paradigm" not in shown


# ---- what may be loaded -----------------------------------------------------------------------

@pytest.mark.parametrize(("url", "address", "why"), [
    ("http://example.com/", "93.184.216.34", "not an https URL"),
    ("https://example.com:8443/", "93.184.216.34", "not the https port"),
    ("https://user:pw@example.com/", "93.184.216.34", "has credentials in it"),
    ("https://metadata.example/", "169.254.169.254", "not a public address"),   # the cloud's metadata service
    ("https://internal.example/", "10.0.0.7", "not a public address"),
    ("https://localhost/", "127.0.0.1", "not a public address"),
    ("https://example.com/page", "93.184.216.34", ""),
])
def test_only_https_pages_on_public_addresses_are_loaded(monkeypatch, url, address, why):
    from app import search

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (address, 443))])
    assert search._allowed(url) == why


def test_a_redirect_to_a_private_address_is_refused(web, monkeypatch):
    from app import search

    monkeypatch.setattr(search, "_public_host", lambda host: host != "internal.example")
    web.pages["https://a.example/"] = Resp(status=302, headers={"location": "https://internal.example/x"})
    assert search.fetch_text("https://a.example/") == ("", "not a public address")
    assert "FETCH https://internal.example/x" not in web.log  # refused before any request


@pytest.mark.parametrize(("resp", "why"), [
    (Resp(status=403), "the page answered 403"),
    (Resp(body=b"%PDF", headers={"content-type": "application/pdf"}), "not a web page (application/pdf)"),
    (Resp(body=b"x" * 3_000_001), "the page is too big"),
])
def test_pages_that_cant_be_read_say_why(web, resp, why):
    from app import search

    web.pages["https://a.example/"] = resp
    assert search.fetch_text("https://a.example/") == ("", why)


# ---- the search itself -------------------------------------------------------------------------

def test_wikipedia_needs_no_key_and_tavily_joins_when_its_key_is_set(web, monkeypatch):
    from app import search

    web.wiki = ["Python (programming language)"]
    web.pages[WIKI + "Python_%28programming_language%29"] = Resp(body=PYTHON_PAGE.encode())
    assert search.providers() == ["wikipedia"]
    found, errors = search.search("latest stable version of Python")
    assert errors == [] and len(found) == 1 and found[0].title == "Python (programming language) (Wikipedia)"
    assert "Stable release 3.15.0" in found[0].text
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    web.tavily = [{"url": "https://docs.python.org/3/whatsnew/", "title": "What's New", "content": "3.15"},
                  {"url": "https://huggingface.co/datasets/x", "title": "benchmark copy", "content": "answers"},
                  {"url": "http://plain.example/", "title": "not https", "content": "x"}]
    assert search.providers() == ["wikipedia", "tavily"]
    found, _ = search.search("latest stable version of Python")
    assert [s.provider for s in found] == ["wikipedia", "tavily"]  # the benchmark copy and the http page are left out
    assert found[1].error == "the page answered 404"


def test_tavily_stops_at_its_daily_limit(web, monkeypatch):
    from app import search
    from app.config import registry

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setitem(registry()["search"], "tavily", {"daily_limit": 2})
    monkeypatch.setattr(search, "_tavily_used", {})
    errors = [search.search("q")[1] for _ in range(3)]
    assert errors[:2] == [[], []] and errors[2] == ["tavily: tavily daily limit reached"]
    assert sum(line.startswith("POST") for line in web.log) == 2


# ---- the short-fact route, settled by a source ------------------------------------------------

def auto(question="What is the latest stable version of Python?", **kw):
    from app.orchestrator import run_pipeline

    return run_pipeline(question, "", A, B, "auto", None, **kw)


def settle_reply(answer, source, quote):
    return json.dumps({"answer": answer, "source": source, "quote": quote})


@pytest.fixture
def python_page(web):
    web.wiki = ["Python (programming language)"]
    web.pages[WIKI + "Python_%28programming_language%29"] = Resp(body=PYTHON_PAGE.encode())
    return web


def test_a_dispute_is_settled_by_a_quote_found_on_a_loaded_page(llm, python_page):
    llm.on(provider="alpha", purpose="answer", reply="[current]\n3.14")
    llm.on(provider="beta", purpose="answer", reply="[current]\n3.13")
    llm.on(provider="judge", purpose="settle", reply=settle_reply("3.15", 1, "Stable release 3.15.0 / 9 October 2026"))
    out = auto()
    assert out["outcome"] == "sourced" and out["final"] == "3.15" and out["route"] == "fact"
    assert out["sources"] == [{"answer": "3.15", "quote": "Stable release 3.15.0 / 9 October 2026",
                               "url": WIKI + "Python_%28programming_language%29",
                               "title": "Python (programming language) (Wikipedia)", "provider": "wikipedia"}]
    assert out["route_note"] == "alpha/a1: 3.14 · beta/b1: 3.13. A source settled it."
    assert stages(out, "search")[0]["sources"][0]["loaded"] is True
    assert stages(out, "settle")[0]["checked"] is True
    settle = next(p for p in llm.prompts if "You settle a short factual question" in p)
    assert "- 3.14\n- 3.13" in settle and "alpha" not in settle   # the answers, not who gave them
    assert "Stable release 3.15.0" in settle                         # the passage, as found on the page


@pytest.mark.parametrize(("reply", "why"), [
    (settle_reply("3.16", 1, "Stable release 3.16.0 / 9 October 2026"), "the quote isn't on the page"),
    (settle_reply("3.16", 1, "Stable release 3.15.0 / 9 October 2026"), "the quote doesn't state that answer"),
    (settle_reply("3.15", 7, "Stable release 3.15.0 / 9 October 2026"), "it named no source it was shown"),
    (settle_reply("?", 0, ""), "the sources don't settle it"),
])
def test_an_unchecked_quote_settles_nothing(llm, python_page, reply, why):
    llm.on(provider="alpha", purpose="answer", reply="[fact]\nSydney")
    llm.on(provider="beta", purpose="answer", reply="[fact]\nCanberra")
    llm.on(purpose="same", reply='{"same": false}')
    llm.on(provider="judge", purpose="settle", reply=reply)
    out = auto("What is the capital of Australia?")
    assert out["outcome"] == "unsure" and out["final"] == "Not sure." and out["sources"] == []
    assert stages(out, "settle")[0]["why"] == why
    assert out["route_note"] == "alpha/a1: Sydney · beta/b1: Canberra. No source settled it."


def test_two_models_agreeing_on_a_short_fact_need_no_search(llm, python_page):
    llm.on(purpose="answer", reply="[fact]\nCanberra")
    out = auto("What is the capital of Australia?")
    assert out["outcome"] == "agreed" and python_page.log == [] and llm.count(purpose="settle") == 0


def test_a_fact_that_can_change_is_searched_even_when_both_agree(llm, python_page):
    llm.on(purpose="answer", reply="[current]\n3.14")
    llm.on(provider="judge", purpose="settle", reply=settle_reply("3.15", 1, "Stable release 3.15.0 / 9 October 2026"))
    out = auto()
    assert out["outcome"] == "sourced" and out["final"] == "3.15"   # both models were out of date


def test_a_fact_that_can_change_and_no_source_confirms_says_so(llm, python_page):
    llm.on(purpose="answer", reply="[current]\n3.14")
    llm.on(provider="judge", purpose="settle", reply=settle_reply("?", 0, ""))
    out = auto()
    assert out["outcome"] == "agreed" and out["final"] == "3.14"
    assert out["route_note"].endswith("It can change over time, and the search didn't confirm it.")


def test_without_search_a_fact_that_can_change_says_nothing_checked_it(llm):
    llm.on(purpose="answer", reply="[current]\n3.14")
    out = auto()
    assert out["outcome"] == "agreed"
    assert out["route_note"].endswith("It can change over time, and no search is set up to check it.")


def test_when_no_model_is_sure_a_source_can_still_answer(llm, python_page):
    llm.on(purpose="answer", reply="[current]\n?")
    llm.on(provider="judge", purpose="settle", reply=settle_reply("3.15.0", 1, "Stable release 3.15.0"))
    out = auto()
    assert out["outcome"] == "sourced" and out["final"] == "3.15.0"
    assert "- (none: no model was sure)" in next(p for p in llm.prompts if "You settle" in p)


def test_in_a_conversation_the_search_query_is_written_with_the_context(llm, python_page):
    from app.orchestrator import run_pipeline

    llm.on(purpose="answer", reply="[current]\n?")
    llm.on(purpose="query", reply='"latest stable Python release"')
    llm.on(provider="judge", purpose="settle", reply=settle_reply("?", 0, ""))
    run_pipeline("And its latest stable version?", "User: Tell me about Python.\n\nAssistant: A language.", A, B,
                 "auto", None)
    assert any(line.endswith("latest stable Python release") for line in python_page.log)


def test_a_search_that_finds_nothing_usable_settles_nothing(llm, web):
    web.wiki = ["Nothing Useful"]   # its page won't load
    llm.on(provider="alpha", purpose="answer", reply="[fact]\nSydney")
    llm.on(provider="beta", purpose="answer", reply="[fact]\nCanberra")
    llm.on(purpose="same", reply='{"same": false}')
    out = auto("What is the capital of Australia?")
    assert out["outcome"] == "unsure" and llm.count(purpose="settle") == 0
    assert stages(out, "search")[0]["sources"][0]["error"] == "the page answered 404"


def test_the_benchmark_is_closed_book_unless_its_config_asks(env, tmp_path):
    from app.config import registry
    from bench import runner

    runner.setup_app({"providers": {}, "pipeline": {}}, str(tmp_path))
    assert registry()["search"]["enabled"] is False
    runner.setup_app({"providers": {}, "pipeline": {}, "search": {"enabled": True}}, str(tmp_path))
    assert registry()["search"]["enabled"] is True


def test_sources_are_shown_as_checked_links(client, admin, llm, python_page):
    from tests.test_web import ask, wait_done

    llm.on(provider="alpha", purpose="answer", reply="[current]\n3.14")
    llm.on(provider="beta", purpose="answer", reply="[current]\n3.13")
    llm.on(provider="judge", purpose="settle", reply=settle_reply("3.15", 1, "Stable release 3.15.0 / 9 October 2026"))
    r = ask(client, admin, question="What is the latest stable version of Python?", mode="auto")
    d = wait_done(client, r.json()["message_id"])
    assert d["content"] == "3.15" and d["pipeline"]["outcome"] == "sourced"
    page = client.get(f"/chat/{r.json()['thread_id']}").text
    assert 'class="chip sourced">source found<' in page
    assert f'href="{WIKI}Python_%28programming_language%29"' in page and 'rel="noopener noreferrer nofollow"' in page
    assert "Stable release 3.15.0 / 9 October 2026" in page
