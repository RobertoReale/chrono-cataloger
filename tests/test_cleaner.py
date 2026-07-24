from datetime import datetime, timezone

from src.cleaner import clean, normalize_url
from src.models import HistoryEntry, datetime_to_webkit_micros


def _entry(url, title="t", vc=1, day=1):
    micros = datetime_to_webkit_micros(datetime(2026, 7, day, 12, tzinfo=timezone.utc))
    return HistoryEntry(url=url, title=title, visit_count=vc, last_visit_micros=micros)


def test_normalize_strips_tracking_and_www_and_fragment():
    url = "https://www.Example.com/page/?utm_source=x&fbclid=y#section"
    assert normalize_url(url, strip_query=False) == "https://example.com/page"


def test_normalize_strip_query_true():
    assert normalize_url("https://x.com/a?b=c", strip_query=True) == "https://x.com/a"


def test_normalize_keeps_content_params_even_when_stripping():
    # without ?v=... every video would normalize to the same "youtube.com/watch"
    out = normalize_url(
        "https://www.youtube.com/watch?v=abc&pp=track&list=xyz", strip_query=True
    )
    assert out == "https://youtube.com/watch?v=abc"


def test_normalize_keeps_meaningful_query_when_not_stripping():
    out = normalize_url("https://youtube.com/watch?v=abc&utm_source=x", strip_query=False)
    assert "v=abc" in out
    assert "utm_source" not in out


def test_domain_blacklist():
    entries = [_entry("https://mail.google.com/inbox"), _entry("https://good.com/a")]
    cfg = {"domain_blacklist": ["mail.google.com"], "strip_query_params": True}
    out = clean(entries, cfg)
    assert len(out) == 1
    assert "good.com" in out[0].normalized_url


def test_non_web_schemes_are_dropped():
    entries = [
        _entry("chrome-extension://bbomjaikkcabgmfaomdichgcodnaeecf/panel.html"),
        _entry("file:///C:/Users/x/report.pdf"),
        _entry("https://good.com/a"),
    ]
    out = clean(entries, {"strip_query_params": True})
    assert [e.normalized_url for e in out] == ["https://good.com/a"]


def test_keyword_blacklist_uses_original_url():
    # 'login' is in the query: it must be dropped even with strip_query enabled
    entries = [_entry("https://site.com/x?next=/login")]
    cfg = {"url_keyword_blacklist": ["login"], "strip_query_params": True}
    assert clean(entries, cfg) == []


def test_dedup_sums_visit_counts_and_keeps_latest():
    entries = [
        _entry("https://x.com/a", vc=2, day=1),
        _entry("https://x.com/a", vc=3, day=5),
    ]
    out = clean(entries, {"strip_query_params": True})
    assert len(out) == 1
    assert out[0].visit_count == 5


def test_min_visit_count_filter():
    entries = [_entry("https://x.com/a", vc=1)]
    out = clean(entries, {"min_visit_count": 3, "strip_query_params": True})
    assert out == []
