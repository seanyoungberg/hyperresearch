"""Offline tests for per-process auth-profile cloning + driver-death retry classification.

The parallel-research topology runs several fetcher processes at once. Chromium
permits only one process per user_data_dir, so a shared auth profile makes every
browser abort. The provider clones the profile to a unique per-process dir; these
tests verify the clone preserves auth state, skips cache, strips locks, and that the
provider wires the clone (not the shared source) into its BrowserConfig.
"""

import pathlib
import shutil

from hyperresearch.web.crawl4ai_provider import (
    Crawl4AIProvider,
    _clone_profile_dir,
    _is_driver_death,
)


def _make_fake_profile(tmp_path):
    src = tmp_path / "profile"
    (src / "Default").mkdir(parents=True)
    (src / "Default" / "Cookies").write_text("cookie-data")
    (src / "Default" / "Local Storage").mkdir()
    (src / "GPUCache").mkdir()
    (src / "GPUCache" / "data_0").write_text("x" * 100)
    (src / "Code Cache").mkdir()
    (src / "Code Cache" / "blob").write_text("y" * 100)
    (src / "SingletonLock").write_text("lock")
    return src


def test_clone_preserves_auth_skips_cache_and_locks(tmp_path):
    src = _make_fake_profile(tmp_path)
    dst = _clone_profile_dir(str(src))
    try:
        assert dst != str(src)
        assert "hpr-prof-" in dst
        # auth state copied
        assert (pathlib.Path(dst) / "Default" / "Cookies").read_text() == "cookie-data"
        assert (pathlib.Path(dst) / "Default" / "Local Storage").is_dir()
        # cache skipped (chromium rebuilds it)
        assert not (pathlib.Path(dst) / "GPUCache").exists()
        assert not (pathlib.Path(dst) / "Code Cache").exists()
        # single-process lock stripped so the clone launches clean
        assert not (pathlib.Path(dst) / "SingletonLock").exists()
    finally:
        shutil.rmtree(dst, ignore_errors=True)


def test_two_clones_are_distinct_dirs(tmp_path):
    src = _make_fake_profile(tmp_path)
    a = _clone_profile_dir(str(src))
    b = _clone_profile_dir(str(src))
    try:
        assert a != b  # each process gets its own dir → no chromium contention
    finally:
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


def test_provider_wires_clone_not_source(tmp_path):
    src = _make_fake_profile(tmp_path)
    provider = Crawl4AIProvider(user_data_dir=str(src))
    try:
        assert provider._source_data_dir == str(src)
        assert provider._data_dir != str(src)
        assert "hpr-prof-" in provider._data_dir
        assert provider._browser_config.user_data_dir == provider._data_dir
        assert (pathlib.Path(provider._data_dir) / "Default" / "Cookies").exists()
    finally:
        shutil.rmtree(provider._data_dir, ignore_errors=True)


def test_no_profile_no_clone():
    provider = Crawl4AIProvider()
    assert provider._data_dir is None
    assert provider._source_data_dir is None


def test_each_managed_provider_gets_unique_cdp_port(tmp_path):
    src = _make_fake_profile(tmp_path)
    p1 = Crawl4AIProvider(user_data_dir=str(src))
    p2 = Crawl4AIProvider(user_data_dir=str(src))
    try:
        # distinct CDP ports → parallel managed-browser processes don't collide on 9222
        assert p1._browser_config.debugging_port != p2._browser_config.debugging_port
    finally:
        shutil.rmtree(p1._data_dir, ignore_errors=True)
        shutil.rmtree(p2._data_dir, ignore_errors=True)


def test_driver_death_classifier_targets_only_driver_failures():
    # retryable — the browser driver itself died
    assert _is_driver_death("Connection closed while reading from the driver")
    assert _is_driver_death("net::ERR_ABORTED at https://example.com")
    assert _is_driver_death("Target closed")
    assert _is_driver_death("Browser has been closed")
    # NOT retryable — clean per-site outcomes
    assert not _is_driver_death("Blocked by anti-bot protection: Cloudflare JS challenge")
    assert not _is_driver_death("net::ERR_NAME_NOT_RESOLVED")
    assert not _is_driver_death("HTTP 404 Not Found")
