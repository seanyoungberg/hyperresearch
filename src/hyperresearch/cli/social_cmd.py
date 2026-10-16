"""Find threads on Reddit and Hacker News through their open archives.

Web search rarely returns Reddit threads (Claude's search refuses reddit.com),
so a sweep that needs what practitioners say asks these directly, then fetches
the threads it picks with `fetch-batch`, which reads them through the same
open routes.
"""

from __future__ import annotations

import typer

from hyperresearch.cli._output import console, output
from hyperresearch.models.output import error, success

app = typer.Typer()


def _emit(hits: list[dict], json_output: bool, errors: list[dict] | None = None) -> None:
    if json_output:
        env = success(hits, count=len(hits))
        payload = env.model_dump() if hasattr(env, "model_dump") else dict(env)
        payload["errors"] = errors or []
        import json as _json
        import sys as _sys

        _sys.stdout.write(_json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return
    for e in errors or []:
        console.print(f"[yellow]Not searched:[/] r/{e['subreddit']}: {e['error']}")
    for h in hits:
        console.print(f"[bold]{h['score']}[/] pts · {h['comments']} comments · {h['where']} · {h['date']}  {h['title']}\n  {h['url']}")


def _fail(e: Exception, json_output: bool) -> None:
    if json_output:
        output(error(str(e), "SOCIAL_SEARCH_FAILED"), json_mode=True)
    else:
        console.print(f"[red]Search failed:[/] {e}")
    raise typer.Exit(1)


@app.command("reddit")
def social_reddit(
    query: str = typer.Argument(..., help="Words to find in post titles and text"),
    subreddit: list[str] = typer.Option(..., "--subreddit", "-r", help="Subreddit to search (repeat for several)"),
    after: str | None = typer.Option(None, "--after", help="YYYY-MM-DD"),
    before: str | None = typer.Option(None, "--before", help="YYYY-MM-DD"),
    limit: int = typer.Option(25, "--limit", "-n"),
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
) -> None:
    """Reddit posts matching QUERY in the named subreddits, through the Arctic Shift archive."""
    from hyperresearch.web.site_routes import search_reddit

    try:
        hits, errors = search_reddit(query, subreddit, after=after, before=before, limit=limit)
        if errors and not hits:
            raise RuntimeError("; ".join(f"r/{e['subreddit']}: {e['error']}" for e in errors))
        _emit(hits, json_output, errors)
    except Exception as e:
        _fail(e, json_output)


@app.command("hn")
def social_hn(
    query: str = typer.Argument(..., help="Search words"),
    after: str | None = typer.Option(None, "--after", help="YYYY-MM-DD"),
    before: str | None = typer.Option(None, "--before", help="YYYY-MM-DD"),
    limit: int = typer.Option(25, "--limit", "-n"),
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
) -> None:
    """Hacker News stories matching QUERY, through Algolia."""
    from hyperresearch.web.site_routes import search_hn

    try:
        _emit(search_hn(query, after=after, before=before, limit=limit), json_output)
    except Exception as e:
        _fail(e, json_output)
