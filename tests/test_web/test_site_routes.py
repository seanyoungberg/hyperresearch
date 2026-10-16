"""Open site routes: URL matching, rendering from recorded API shapes, and fallback."""

from __future__ import annotations

import pytest

from hyperresearch.web import site_routes as sr
from hyperresearch.web.base import WebResult


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("https://www.reddit.com/r/ClaudeCode/comments/1x03lg8/introducing_claude/", "1x03lg8"),
        ("https://old.reddit.com/r/codex/comments/1X0KZBH/x/abc123/", "1x0kzbh"),
        ("https://redd.it/1x03lg8", "1x03lg8"),
        ("https://www.reddit.com/r/ClaudeCode/", None),
        ("https://evilreddit.com/r/a/comments/1x03lg8/", None),
    ],
)
def test_reddit_id(url, want):
    assert sr.reddit_id(url) == want


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("https://x.com/ClaudeDevs/status/2107895955144208813", ("ClaudeDevs", "2107895955144208813")),
        ("https://twitter.com/a_b/status/123456789?s=20", ("a_b", "123456789")),
        ("https://x.com/ClaudeDevs", None),
        ("https://notx.com/a/status/123456789", None),
    ],
)
def test_x_status(url, want):
    assert sr.x_status(url) == want


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("https://www.youtube.com/watch?v=en0GuyhieQk&t=3", "en0GuyhieQk"),
        ("https://youtu.be/en0GuyhieQk", "en0GuyhieQk"),
        ("https://youtube.com/shorts/en0GuyhieQk", "en0GuyhieQk"),
        ("https://www.youtube.com/@anthropic-ai", None),
    ],
)
def test_youtube_id(url, want):
    assert sr.youtube_id(url) == want


def test_hn_id():
    assert sr.hn_id("https://news.ycombinator.com/item?id=49996437") == "49996437"
    assert sr.hn_id("https://news.ycombinator.com/news") is None


POST = {"data": [{"id": "1x03lg8", "subreddit": "ClaudeCode", "author": "ClaudeOfficial",
                  "title": "Introducing Claude Haiku 5.5", "score": 948, "num_comments": 3,
                  "created_utc": 1791396165, "url": "https://v.redd.it/f3", "is_self": False,
                  "selftext": "Cheapest small model yet."}]}
TREE = {"data": [
    {"kind": "t1", "data": {"author": "low", "score": 2, "body": "meh", "created_utc": 1791396200, "replies": ""}},
    {"kind": "t1", "data": {"author": "top", "score": 90, "body": "OAuth login broke for me, captcha too",
                            "created_utc": 1791396300,
                            "replies": {"data": {"children": [
                                {"kind": "t1", "data": {"author": "child", "score": 5, "body": "same",
                                                        "created_utc": 1791396400, "replies": ""}}]}}}},
    {"kind": "more", "data": {"count": 1}},
]}


def fake_json(table):
    def get(url):
        for key, value in table.items():
            if key in url:
                return value
        raise sr.RouteError(f"unexpected {url}")
    return get


def test_reddit_renders_post_and_sorted_threaded_comments():
    url = "https://old.reddit.com/r/ClaudeCode/comments/1x03lg8/x/"
    r = sr.fetch_reddit(url, fake_json({"posts/ids": POST, "comments/tree": TREE}))
    assert r.url == url  # batch fetch keys results by the URL it asked for
    assert r.metadata["canonical_url"] == "https://www.reddit.com/r/ClaudeCode/comments/1x03lg8/"
    assert r.content.index("u/top") < r.content.index("u/child") < r.content.index("u/low")
    assert "Link: https://v.redd.it/f3" in r.content and "more were not loaded" in r.content
    # An API-read thread that discusses logins or captchas is not a wall or a bot page.
    assert r.looks_like_login_wall(url) is False and r.looks_like_junk() is None


def test_x_renders_post_quote_and_counts():
    t = {"text": "Haiku 5.5 is out https://t.co/abc", "created_at": "2026-10-07T17:00:00.000Z",
         "favorite_count": 13061, "conversation_count": 311, "user": {"screen_name": "ClaudeDevs", "name": "Claude Devs"},
         "quoted_tweet": {"text": "Introducing Haiku", "user": {"screen_name": "claudeai"}}}
    r = sr.fetch_x("https://twitter.com/ClaudeDevs/status/2107895955144208813", fake_json({"syndication": t}))
    assert "13061 likes" in r.content and "Quoting @claudeai" in r.content and "t.co" not in r.content


def test_youtube_needs_a_transcript():
    meta = {"title": "Introducing Claude Dashboards", "author_name": "Claude"}
    r = sr.fetch_youtube("https://youtu.be/en0GuyhieQk", fake_json({"oembed": meta}), transcript=lambda v: "hello world")
    assert "## Transcript" in r.content and r.metadata["author"] == "Claude"
    with pytest.raises(sr.RouteError):
        sr.fetch_youtube("https://youtu.be/en0GuyhieQk", fake_json({"oembed": meta}), transcript=lambda v: None)


def test_hn_renders_thread_and_skips_deleted_comments():
    item = {"title": "Claude Haiku 5.5", "author": "pg", "points": 1046, "created_at": "2026-10-07T00:00:00Z",
            "url": "https://anthropic.com/x", "children": [
                {"text": None, "children": [{"text": "<p>nested &amp; kept", "author": "b", "children": []}]},
                {"text": "first", "author": "a", "children": []}]}
    r = sr.fetch_hn("https://news.ycombinator.com/item?id=49996437", fake_json({"items/49996437": item}))
    assert "nested & kept" in r.content and "2 comments" in r.content


class Inner:
    name = "crawl4ai"

    def __init__(self):
        self.seen = []

    def fetch(self, url):
        self.seen.append(url)
        return WebResult(url=url, title="page", content="x" * 500)

    def fetch_many(self, urls):
        self.seen.extend(urls)
        return [WebResult(url=u, title="page", content="x" * 500) for u in urls]


def test_wrapper_routes_falls_back_and_delegates(monkeypatch):
    def boom(url):
        raise sr.RouteError("archive down")

    monkeypatch.setattr(sr, "ROUTES", [("arctic-shift", sr.reddit_id, boom)])
    inner = Inner()
    w = sr.SiteRoutes(inner)
    assert w.name == "crawl4ai"
    w.fetch("https://www.reddit.com/r/a/comments/1x03lg8/")  # route fails -> inner
    w.fetch("https://example.com/")
    assert inner.seen == ["https://www.reddit.com/r/a/comments/1x03lg8/", "https://example.com/"]
    assert hasattr(w, "fetch_many")
    out = w.fetch_many(["https://example.com/a", "https://www.reddit.com/r/a/comments/1x03lg8/"])
    assert len(out) == 2


def test_wrapper_has_no_fetch_many_when_inner_lacks_it():
    class Plain:
        name = "builtin"

        def fetch(self, url):
            return WebResult(url=url, title="t", content="c")

    assert not hasattr(sr.SiteRoutes(Plain()), "fetch_many")


def test_get_provider_wraps_unless_disabled(monkeypatch):
    from hyperresearch.web.base import get_provider

    assert isinstance(get_provider("builtin"), sr.SiteRoutes)
    monkeypatch.setenv("HYPERRESEARCH_SITE_ROUTES", "0")
    assert not isinstance(get_provider("builtin"), sr.SiteRoutes)


def test_search_reddit_ranks_across_subreddits_and_rejects_bad_names():
    pages = {"subreddit=ClaudeCode": {"data": [{"id": "a1", "subreddit": "ClaudeCode", "title": "low", "score": 3, "num_comments": 0}]},
             "subreddit=codex": {"data": [{"id": "b2", "subreddit": "codex", "title": "high", "score": 50, "num_comments": 40}]}}
    hits, errors = sr.search_reddit("haiku", ["ClaudeCode", "codex"], after="2026-10-04", get_json=fake_json(pages))
    assert errors == []
    assert [h["title"] for h in hits] == ["high", "low"]
    assert hits[0]["url"] == "https://www.reddit.com/r/codex/comments/b2/"
    with pytest.raises(ValueError):
        sr.search_reddit("x", ["bad/name"], get_json=fake_json(pages))
    with pytest.raises(ValueError):
        sr.search_reddit("x", ["codex"], after="last week", get_json=fake_json(pages))


def test_search_hn_builds_window_and_links():
    seen = []

    def get(url):
        seen.append(url)
        return {"hits": [{"objectID": "49996437", "title": "Claude Haiku 5.5", "points": 1046, "num_comments": 488}]}

    hits = sr.search_hn("haiku 5.5", after="2026-10-04", before="2026-10-11", get_json=get)
    assert hits[0]["url"] == "https://news.ycombinator.com/item?id=49996437"
    assert "numericFilters=created_at_i%3E" in seen[0]


def test_get_json_retries_a_soft_error_then_raises(monkeypatch):
    from hyperresearch.web.safe_http import SafeResponse

    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return SafeResponse(url=url, status_code=200, headers={}, content=b'{"data": null, "error": "Timeout. Maybe slow down a bit"}')

    monkeypatch.setattr(sr, "safe_get", fake_get)
    monkeypatch.setattr(sr.time, "sleep", lambda s: None)
    with pytest.raises(sr.RouteError, match="slow down"):
        sr._get_json("https://arctic-shift.photon-reddit.com/api/posts/search?x=1")
    assert len(calls) == 3


def test_search_reddit_falls_back_to_titles_then_reports_the_subreddit():
    def get(url):
        if "subreddit=ClaudeAI&query=" in url:
            raise sr.RouteError("Timeout. Maybe slow down a bit")
        if "subreddit=ClaudeAI&title=" in url:
            return {"data": [{"id": "c3", "subreddit": "ClaudeAI", "title": "by title", "score": 9, "num_comments": 1}]}
        raise sr.RouteError("down")

    hits, errors = sr.search_reddit("haiku", ["ClaudeAI", "codex"], get_json=get)
    assert [h["matched"] for h in hits] == ["title"]
    assert errors == [{"subreddit": "codex", "error": "down"}]
