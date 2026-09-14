"""Domain-agnostic fallback extractor for sites with no dedicated extractor.

Registered last (see registry.py), so any of the tuned, site-specific
extractors still get first refusal on their own known domains. This one's
`matches()` always returns True, and it never has domains configured up
front: common.py's GenericListingExtractor scopes itself to whatever host
the pasted listing URL is actually on (see its `domains=None` handling),
so the same anti-cross-domain-ad filtering the tuned extractors get for
free still applies here without needing to know the site in advance.

The video-URL heuristic below is the same one the tuned extractors already
use (a shallow path segment not matching a common boilerplate prefix, then
common.py's own >=2-occurrence-on-the-page threshold to separate real
content links from one-off nav links) -- just without any site-specific
tuning (no custom anchor scoping, no bespoke pagination). That makes this a
best-effort scan, not a guarantee: a site whose real content lives at a
deeper path, or whose listing needs JS to render, still won't work here and
would need its own dedicated extractor.
"""

from __future__ import annotations

from veloci_engine.extractors.common import GenericListingExtractor

_EXCLUDED_PREFIXES = (
    "/category/",
    "/tag/",
    "/tags/",
    "/page/",
    "/author/",
    "/model/",
    "/models/",
    "/wp-",
    "/feed",
    "/search",
    "/advanced-search",
    "/login",
    "/signin",
    "/signup",
    "/register",
    "/cart",
    "/checkout",
    "/account",
    "/about",
    "/contact",
    "/privacy",
    "/terms",
    "/faq",
    "/sitemap",
    "/rss",
)

# Same shape as the tuned extractors: any single shallow path segment not
# starting with a known non-content prefix. Left loose (no charset
# restriction) so percent-encoded/unicode slugs still match.
VIDEO_URL_PATTERN = r"^/(?!" + "|".join(p.lstrip("/") for p in _EXCLUDED_PREFIXES) + r")[^/]+/?$"

EXTRACTOR = GenericListingExtractor(
    name="fallback",
    domains=None,
    video_url_pattern=VIDEO_URL_PATTERN,
)
