"""Open routes for sites a headless browser cannot read.

Reddit, X, YouTube and Hacker News either wall off anonymous browsers or hold
the useful part (comments, transcripts) behind script. Each has a keyless,
public door that returns the same content as data:

- Reddit thread    -> Arctic Shift archive: the post and its comment tree
- X post           -> X's public embed endpoint: text, author, counts, quote
- YouTube video    -> oEmbed for title and channel, plus the English transcript
- Hacker News item -> Algolia's item API: the story and its whole thread

``SiteRoutes`` wraps any provider. A URL that matches a route is fetched
through that route; anything else, and any route that fails, goes to the
wrapped provider unchanged, so blocked pages still reach the escalation lane.
Every request goes through ``safe_get`` (SSRF gate, size cap).
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import parse_qs, quote, urlparse

from dataclasses import dataclass

from hyperresearch.web.base import DEFAULT_GATES, WebResult
from hyperresearch.web.safe_http import safe_get

ARCTIC = "https://arctic-shift.photon-reddit.com/api"
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_CONTENT_CHARS = 120_000  # a long thread is cut here, with a note saying so
MAX_COMMENTS = 400

GetJSON = Callable[[str], object]


@dataclass
class RoutedResult(WebResult):
    """Content read from a site's API, never an interstitial.

    The login-wall and bot-page checks match words like "auth", "captcha" or
    "access denied" anywhere in the title or opening text. On a browser page that
    means a wall; in an API-read thread it is just what people were discussing,
    so a routed result is judged only on being empty.
    """

    def looks_like_login_wall(self, original_url, gates=None) -> bool:
        return False

    def looks_like_junk(self, gates=None) -> str | None:
        gates = gates or DEFAULT_GATES
        if len((self.content or "").strip()) < gates.min_content_chars:
            return "Empty or near-empty content"
        return None


class RouteError(Exception):
    """A route could not produce content; the caller falls back."""


def _get_json(url: str, *, tries: int = 3) -> object:
    """GET JSON through the SSRF gate, waiting out Arctic Shift's rate limit."""
    for attempt in range(tries):
        resp = safe_get(url, max_bytes=MAX_JSON_BYTES, timeout=30.0,
                        headers={"Accept": "application/json"})
        if resp.status_code in (422, 429) and attempt < tries - 1:
            wait = resp.headers.get("x-ratelimit-reset", "")
            time.sleep(min(int(wait) if wait.isdigit() else 5, 30) + 1)
            continue
        if resp.status_code != 200:
            raise RouteError(f"HTTP {resp.status_code} from {urlparse(url).netloc}")
        try:
            body = json.loads(resp.text)
        except ValueError as exc:
            raise RouteError(f"bad JSON from {urlparse(url).netloc}: {exc}") from exc
        # Arctic Shift reports an overloaded query as 200 {"data": null, "error": ...}.
        # Treat it like a rate limit, never as "no results".
        if isinstance(body, dict) and body.get("error") and body.get("data") is None:
            if attempt < tries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise RouteError(f"{urlparse(url).netloc}: {body['error']}")
        return body
    raise RouteError(f"rate limited by {urlparse(url).netloc}")


def _iso(ts: object) -> str:
    try:
        return datetime.fromtimestamp(float(ts), UTC).strftime("%Y-%m-%d %H:%M UTC")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(ts or "")


def _strip_html(text: str) -> str:
    text = re.sub(r"<p>", "\n\n", text or "")
    text = re.sub(r'<a href="([^"]+)"[^>]*>[^<]*</a>', r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _cap(lines: list[str]) -> str:
    out = "\n".join(lines)
    if len(out) > MAX_CONTENT_CHARS:
        out = out[:MAX_CONTENT_CHARS] + "\n\n[cut at the length limit]"
    return out


# --------------------------------------------------------------------------- Reddit

_REDDIT = re.compile(r"^(?:www\.|old\.|new\.|np\.|m\.)?reddit\.com$")
_REDDIT_THREAD = re.compile(r"/comments/([a-z0-9]{4,10})(?:/|$)", re.I)


def reddit_id(url: str) -> str | None:
    p = urlparse(url)
    host = p.netloc.lower()
    if host == "redd.it":
        m = re.match(r"^/([a-z0-9]{4,10})/?$", p.path, re.I)
        return m.group(1).lower() if m else None
    if _REDDIT.match(host):
        m = _REDDIT_THREAD.search(p.path)
        return m.group(1).lower() if m else None
    return None


def _walk_comments(children: list, depth: int, lines: list[str], seen: list[int]) -> None:
    for c in sorted((c for c in children if c.get("kind") == "t1"),
                    key=lambda c: -(c["data"].get("score") or 0)):
        if seen[0] >= MAX_COMMENTS:
            return
        d = c["data"]
        seen[0] += 1
        body = (d.get("body") or "").strip().replace("\n", "\n" + "  " * depth + "> ")
        lines.append(f"{'  ' * depth}- **u/{d.get('author')}** ({d.get('score')} points, {_iso(d.get('created_utc'))})")
        lines.append(f"{'  ' * depth}  > {body}")
        replies = d.get("replies")
        if isinstance(replies, dict):
            _walk_comments(replies.get("data", {}).get("children", []), depth + 1, lines, seen)


def fetch_reddit(url: str, get_json: GetJSON = _get_json) -> WebResult:
    pid = reddit_id(url)
    if not pid:
        raise RouteError("not a Reddit thread URL")
    posts = (get_json(f"{ARCTIC}/posts/ids?ids={pid}") or {}).get("data") or []  # type: ignore[union-attr]
    if not posts:
        raise RouteError(f"Arctic Shift has no post {pid}")
    p = posts[0]
    tree = (get_json(f"{ARCTIC}/comments/tree?link_id=t3_{pid}&limit={MAX_COMMENTS}") or {}).get("data") or []  # type: ignore[union-attr]
    permalink = f"https://www.reddit.com/r/{p.get('subreddit')}/comments/{pid}/"
    lines = [
        f"# {p.get('title')}",
        "",
        f"r/{p.get('subreddit')} · posted by u/{p.get('author')} · {_iso(p.get('created_utc'))} · "
        f"{p.get('score')} points · {p.get('num_comments')} comments",
        "",
    ]
    if p.get("selftext"):
        lines += [p["selftext"].strip(), ""]
    if not p.get("is_self") and p.get("url") and p.get("url") != permalink:
        lines += [f"Link: {p['url']}", ""]
    seen = [0]
    lines.append("## Comments")
    lines.append("")
    _walk_comments(tree, 0, lines, seen)
    more = sum(1 for c in tree if c.get("kind") == "more")
    lines += ["", f"(Read from the Arctic Shift archive of Reddit: {seen[0]} comments shown"
              f"{', more were not loaded' if more or seen[0] >= MAX_COMMENTS else ''}. Scores settle about 36 hours after posting.)"]
    return RoutedResult(
        url=url, title=p.get("title") or f"Reddit thread {pid}", content=_cap(lines),
        metadata={"author": p.get("author"), "date": _iso(p.get("created_utc")), "domain": "reddit.com",
                  "subreddit": p.get("subreddit"), "score": p.get("score"),
                  "num_comments": p.get("num_comments"), "fetch_route": "arctic-shift", "canonical_url": permalink},
    )


# --------------------------------------------------------------------------- X

_X = re.compile(r"^(?:www\.|mobile\.)?(?:x|twitter)\.com$")
_X_STATUS = re.compile(r"^/(\w{1,15})/status(?:es)?/(\d{5,25})(?:/|$)")


def x_status(url: str) -> tuple[str, str] | None:
    p = urlparse(url)
    if not _X.match(p.netloc.lower()):
        return None
    m = _X_STATUS.match(p.path)
    return (m.group(1), m.group(2)) if m else None


def fetch_x(url: str, get_json: GetJSON = _get_json) -> WebResult:
    st = x_status(url)
    if not st:
        raise RouteError("not an X status URL")
    t = get_json(f"https://cdn.syndication.twimg.com/tweet-result?id={st[1]}&token=a")
    if not isinstance(t, dict) or not t.get("text"):
        raise RouteError(f"X embed endpoint has no post {st[1]}")
    user = t.get("user") or {}
    handle = user.get("screen_name") or st[0]
    text = re.sub(r"\s*https://t\.co/\w+$", "", t["text"])
    lines = [f"# @{handle}: {text[:90]}", "",
             f"{user.get('name') or handle} (@{handle}) · {t.get('created_at', '')} · "
             f"{t.get('favorite_count', 0)} likes · {t.get('conversation_count', 0)} replies", "", text, ""]
    q = t.get("quoted_tweet")
    if isinstance(q, dict) and q.get("text"):
        lines += [f"Quoting @{(q.get('user') or {}).get('screen_name')}:", "", "> " + q["text"].replace("\n", "\n> "), ""]
    for e in (t.get("entities") or {}).get("urls") or []:
        if e.get("expanded_url"):
            lines.append(f"Link: {e['expanded_url']}")
    for m in t.get("mediaDetails") or []:
        if m.get("media_url_https"):
            lines.append(f"Media: {m['media_url_https']}")
    lines += ["", "(Read from X's public embed endpoint: the post only; replies and threads need a login or the X API.)"]
    canonical = f"https://x.com/{handle}/status/{st[1]}"
    return RoutedResult(
        url=url, title=f"@{handle}: {text[:90]}", content=_cap(lines),
        metadata={"author": handle, "date": t.get("created_at"), "domain": "x.com",
                  "likes": t.get("favorite_count"), "replies": t.get("conversation_count"), "fetch_route": "x-embed", "canonical_url": canonical},
    )


# --------------------------------------------------------------------------- YouTube

_YT = re.compile(r"^(?:www\.|m\.|music\.)?youtube\.com$")


def youtube_id(url: str) -> str | None:
    p = urlparse(url)
    host = p.netloc.lower()
    if host == "youtu.be":
        vid = p.path.strip("/").split("/")[0]
    elif _YT.match(host):
        if p.path == "/watch":
            vid = (parse_qs(p.query).get("v") or [""])[0]
        else:
            m = re.match(r"^/(?:shorts|live|embed)/([\w-]{11})", p.path)
            vid = m.group(1) if m else ""
    else:
        return None
    return vid if re.fullmatch(r"[\w-]{11}", vid or "") else None


def _transcript(vid: str) -> str | None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None
    try:
        fetched = YouTubeTranscriptApi().fetch(vid, languages=["en", "en-US", "en-GB"])
    except Exception:  # no English track, disabled captions, blocked network
        return None
    return " ".join(s.text.replace("\n", " ") for s in fetched).strip() or None


def fetch_youtube(url: str, get_json: GetJSON = _get_json,
                  transcript: Callable[[str], str | None] = _transcript) -> WebResult:
    vid = youtube_id(url)
    if not vid:
        raise RouteError("not a YouTube video URL")
    watch = f"https://www.youtube.com/watch?v={vid}"
    meta = get_json(f"https://www.youtube.com/oembed?format=json&url={quote(watch, safe='')}")
    if not isinstance(meta, dict):
        raise RouteError(f"no oEmbed data for {vid}")
    text = transcript(vid)
    if not text:
        raise RouteError(f"no English transcript for {vid}")
    lines = [f"# {meta.get('title')}", "", f"YouTube · {meta.get('author_name')} · {watch}", "",
             "## Transcript", "", text, "",
             "(Transcript from YouTube's English captions, often auto-generated. What is shown on screen is not included.)"]
    return RoutedResult(
        url=url, title=meta.get("title") or f"YouTube {vid}", content=_cap(lines),
        metadata={"author": meta.get("author_name"), "domain": "youtube.com", "fetch_route": "youtube-transcript", "canonical_url": watch},
    )


# --------------------------------------------------------------------------- Hacker News


def hn_id(url: str) -> str | None:
    p = urlparse(url)
    if p.netloc.lower() != "news.ycombinator.com" or p.path != "/item":
        return None
    i = (parse_qs(p.query).get("id") or [""])[0]
    return i if i.isdigit() else None


def _walk_hn(children: list, depth: int, lines: list[str], seen: list[int]) -> None:
    for c in children:
        if seen[0] >= MAX_COMMENTS:
            return
        if not c.get("text"):
            _walk_hn(c.get("children") or [], depth, lines, seen)
            continue
        seen[0] += 1
        body = _strip_html(c["text"]).replace("\n", "\n" + "  " * depth + "> ")
        lines.append(f"{'  ' * depth}- **{c.get('author')}** ({(c.get('created_at') or '')[:10]})")
        lines.append(f"{'  ' * depth}  > {body}")
        _walk_hn(c.get("children") or [], depth + 1, lines, seen)


def fetch_hn(url: str, get_json: GetJSON = _get_json) -> WebResult:
    iid = hn_id(url)
    if not iid:
        raise RouteError("not a Hacker News item URL")
    it = get_json(f"https://hn.algolia.com/api/v1/items/{iid}")
    if not isinstance(it, dict) or not (it.get("title") or it.get("text")):
        raise RouteError(f"Algolia has no item {iid}")
    title = it.get("title") or f"Hacker News comment {iid}"
    lines = [f"# {title}", "",
             f"Hacker News · by {it.get('author')} · {(it.get('created_at') or '')[:10]} · {it.get('points') or 0} points", ""]
    if it.get("url"):
        lines += [f"Link: {it['url']}", ""]
    if it.get("text"):
        lines += [_strip_html(it["text"]), ""]
    seen = [0]
    lines += ["## Comments", ""]
    _walk_hn(it.get("children") or [], 0, lines, seen)
    lines += ["", f"(Read from Hacker News through Algolia's item API: {seen[0]} comments.)"]
    return RoutedResult(
        url=url, title=title, content=_cap(lines),
        metadata={"author": it.get("author"), "date": it.get("created_at"), "domain": "news.ycombinator.com",
                  "points": it.get("points"), "fetch_route": "hn-algolia", "canonical_url": f"https://news.ycombinator.com/item?id={iid}"},
    )


# --------------------------------------------------------------------------- the wrapper

ROUTES: list[tuple[str, Callable[[str], object], Callable[..., WebResult]]] = [
    ("arctic-shift", reddit_id, fetch_reddit),
    ("x-embed", x_status, fetch_x),
    ("youtube-transcript", youtube_id, fetch_youtube),
    ("hn-algolia", hn_id, fetch_hn),
]


def route_for(url: str) -> tuple[str, Callable[..., WebResult]] | None:
    for name, match, fetch in ROUTES:
        try:
            if match(url):
                return name, fetch
        except ValueError:
            return None
    return None


class SiteRoutes:
    """Wrap a provider so routed sites use their open route first."""

    def __init__(self, inner, log: Callable[[str], None] | None = None):
        self._inner = inner
        self._log = log or (lambda _msg: None)
        self.last_route: str | None = None
        if hasattr(inner, "fetch_many"):
            self.fetch_many = self._fetch_many

    def __getattr__(self, name):  # name, search, close and anything else
        return getattr(self._inner, name)

    def fetch(self, url: str) -> WebResult:
        hit = route_for(url)
        self.last_route = None
        if hit:
            name, fetch = hit
            try:
                result = fetch(url)
                self.last_route = name
                return result
            except Exception as exc:  # a route never loses a source: fall back
                self._log(f"{name} route failed for {url}: {exc}; trying {self._inner.name}")
        return self._inner.fetch(url)

    def _fetch_many(self, urls: list[str]) -> list[WebResult]:
        routed = [u for u in urls if route_for(u)]
        rest = [u for u in urls if not route_for(u)]
        results: list[WebResult] = []
        for u in routed:
            try:
                results.append(self.fetch(u))
            except Exception as exc:
                self._log(f"fetch failed for {u}: {exc}")
        if rest:
            results.extend(self._inner.fetch_many(rest))
        return results


# --------------------------------------------------------------------------- discovery


def _day(s: str | None) -> str | None:
    if not s:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ValueError(f"dates are YYYY-MM-DD, got {s!r}")
    return s


def search_reddit(query: str, subreddits: list[str], *, after: str | None = None, before: str | None = None,
                  limit: int = 25, get_json: GetJSON = _get_json) -> tuple[list[dict], list[dict]]:
    """Posts in the named subreddits matching ``query``, best first, and the subreddits that failed.

    Arctic Shift searches text only within a subreddit (or an author), so a
    sweep names the subreddits that carry the topic. A full-text search of a big
    subreddit can time out on the archive's side; that subreddit is retried on
    titles only, and if that fails too it is reported, never silently empty.
    """
    for sub in subreddits:
        if not re.fullmatch(r"\w{2,21}", sub):
            raise ValueError(f"not a subreddit name: {sub!r}")
    window = "".join(f"&{k}={v}" for k, v in (("after", _day(after)), ("before", _day(before))) if v)
    hits: list[dict] = []
    errors: list[dict] = []
    for sub in subreddits:
        posts = None
        for field_name in ("query", "title"):
            url = (f"{ARCTIC}/posts/search?subreddit={sub}&{field_name}={quote(query)}&limit=100&sort=desc{window}"
                   "&fields=id,title,score,num_comments,created_utc,author,subreddit")
            try:
                posts = (get_json(url) or {}).get("data") or []  # type: ignore[union-attr]
                break
            except RouteError as exc:
                last = str(exc)
        if posts is None:
            errors.append({"subreddit": sub, "error": last})
            continue
        for p in posts:
            hits.append({"url": f"https://www.reddit.com/r/{p.get('subreddit')}/comments/{p.get('id')}/",
                         "title": p.get("title"), "score": p.get("score"), "comments": p.get("num_comments"),
                         "date": _iso(p.get("created_utc")), "where": f"r/{p.get('subreddit')}", "author": p.get("author"),
                         "matched": field_name})
    hits.sort(key=lambda h: -((h["score"] or 0) + 2 * (h["comments"] or 0)))
    return hits[:limit], errors


def search_hn(query: str, *, after: str | None = None, before: str | None = None, limit: int = 25,
              get_json: GetJSON = _get_json) -> list[dict]:
    """Hacker News stories matching ``query``, by Algolia relevance."""
    filters = []
    if _day(after):
        filters.append(f"created_at_i>{int(datetime.fromisoformat(after).replace(tzinfo=UTC).timestamp())}")  # type: ignore[arg-type]
    if _day(before):
        filters.append(f"created_at_i<{int(datetime.fromisoformat(before).replace(tzinfo=UTC).timestamp())}")  # type: ignore[arg-type]
    url = f"https://hn.algolia.com/api/v1/search?query={quote(query)}&tags=story&hitsPerPage={min(limit, 100)}"
    if filters:
        url += "&numericFilters=" + quote(",".join(filters))
    out = []
    for h in (get_json(url) or {}).get("hits") or []:  # type: ignore[union-attr]
        out.append({"url": f"https://news.ycombinator.com/item?id={h.get('objectID')}", "title": h.get("title"),
                    "score": h.get("points"), "comments": h.get("num_comments"), "date": (h.get("created_at") or "")[:10],
                    "where": "Hacker News", "author": h.get("author"), "link": h.get("url")})
    return out[:limit]
