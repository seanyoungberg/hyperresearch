"""Regression tests for WebResult.looks_like_junk().

Guards the fix for the Cloudflare false-positive: the bare word "cloudflare"
appears in the body of every legitimate cloudflare.com page and must NOT flag
the page as a bot-detection interstitial.
"""

from hyperresearch.web.base import WebResult


def _result(title: str, content: str) -> WebResult:
    return WebResult(url="https://example.com/", title=title, content=content)


def test_legit_cloudflare_page_is_not_junk():
    """A real cloudflare.com doc page mentions 'cloudflare' throughout but is
    genuine content, not a bot challenge — it must pass the junk filter."""
    body = (
        "Cloudflare Workers let you deploy serverless code instantly across the "
        "globe. This page from developers.cloudflare.com explains how Cloudflare "
        "routes requests, how the Cloudflare cache works, and how to configure a "
        "Cloudflare Worker with Wrangler. " * 20
    )
    assert _result("Cloudflare Workers · Cloudflare Docs", body).looks_like_junk() is None


def test_real_cloudflare_challenge_is_junk():
    """The actual CF interstitial ('Just a moment...' / 'Checking your browser')
    must still be caught."""
    body = (
        "Just a moment... Checking your browser before accessing the site. "
        "This process is automatic. Your browser will redirect to your requested "
        "content shortly. Please allow up to 5 seconds. Ray ID: 8f2a1b3c4d. " * 10
    )
    reason = _result("Just a moment...", body).looks_like_junk()
    assert reason is not None
    assert "Bot detection" in reason


def test_attention_required_title_still_junk():
    """'Attention Required! | Cloudflare' challenge title must still be caught
    (via the 'attention required' signal, not the bare 'cloudflare' word)."""
    body = (
        "Attention Required! You have been blocked. Please complete the security "
        "check to access this site. Sorry for the inconvenience. " * 15
    )
    reason = _result("Attention Required! | Cloudflare", body).looks_like_junk()
    assert reason is not None
    assert "Bot detection" in reason
