"""Local (free) cleaning and deduplication of history entries.

Drastically reduces the volume before the LLM gets involved:
- normalizes URLs (removing tracking params, trailing slashes, etc.);
- applies domain and keyword blacklists;
- deduplicates by normalized URL, summing the visit_counts;
- drops entries below the minimum thresholds.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import HistoryEntry

# Query params known to be tracking-related; removed during normalization.
_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "gclsrc",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "yclid",
    "_ga",
    "ref",
    "ref_src",
    "ref_url",
    "session_id",
    "sessionid",
    "spm",
    "vero_id",
    "wickedid",
}

# Prefixes that mark a param as tracking-related (e.g. utm_source, utm_medium...).
_TRACKING_PREFIXES = ("utm_",)


def _is_tracking_param(key: str) -> bool:
    lk = key.lower()
    if lk in _TRACKING_PARAMS:
        return True
    return any(lk.startswith(p) for p in _TRACKING_PREFIXES)


def normalize_url(url: str, strip_query: bool = True) -> str:
    """Normalize a URL for comparison/dedup.

    - lowercases scheme and host;
    - removes ``www.``;
    - removes the fragment (#...);
    - removes tracking params (always) and — if ``strip_query`` — the whole
      query string;
    - removes the trailing slash from the path (except for the root).
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    if strip_query:
        query = ""
    else:
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if not _is_tracking_param(k)]
        query = urlencode(kept)

    # the fragment is always discarded
    return urlunsplit((scheme, netloc, path, query, ""))


def _domain_of(url: str) -> str:
    netloc = urlsplit(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # drop any port
    return netloc.split(":", 1)[0]


def _domain_blacklisted(domain: str, blacklist: list[str]) -> bool:
    for bad in blacklist:
        bad = bad.lower().strip()
        if not bad:
            continue
        if domain == bad or domain.endswith("." + bad):
            return True
    return False


def _keyword_blacklisted(url: str, keywords: list[str]) -> bool:
    low = url.lower()
    return any(kw.lower().strip() in low for kw in keywords if kw.strip())


def clean(entries: list[HistoryEntry], filtering: dict) -> list[HistoryEntry]:
    """Apply normalization, filters and dedup according to the ``filtering`` section.

    Returns a new list of unique entries (one per normalized URL), sorted by
    ascending ``last_visit``.
    """
    strip_query = bool(filtering.get("strip_query_params", True))
    domain_blacklist = filtering.get("domain_blacklist") or []
    keyword_blacklist = filtering.get("url_keyword_blacklist") or []
    min_visit_count = int(filtering.get("min_visit_count", 1) or 0)

    deduped: dict[str, HistoryEntry] = {}

    for e in entries:
        norm = normalize_url(e.url, strip_query=strip_query)
        if not norm:
            continue

        domain = _domain_of(norm)
        if _domain_blacklisted(domain, domain_blacklist):
            continue
        # The keyword blacklist must be evaluated against the original URL:
        # strip_query may otherwise have removed the very 'login', 'checkout', etc.
        if _keyword_blacklisted(e.url, keyword_blacklist):
            continue

        e.normalized_url = norm

        existing = deduped.get(norm)
        if existing is None:
            # defensive copy, so the input is never mutated in surprising ways
            deduped[norm] = HistoryEntry(
                url=e.url,
                title=e.title,
                visit_count=e.visit_count,
                last_visit_micros=e.last_visit_micros,
                normalized_url=norm,
            )
        else:
            existing.visit_count += e.visit_count
            # keep the most recent visit and its associated title
            if e.last_visit_micros > existing.last_visit_micros:
                existing.last_visit_micros = e.last_visit_micros
                if e.title:
                    existing.title = e.title
                existing.url = e.url

    result = [
        e for e in deduped.values()
        if e.visit_count >= min_visit_count
    ]
    result.sort(key=lambda x: x.last_visit_micros)
    return result
