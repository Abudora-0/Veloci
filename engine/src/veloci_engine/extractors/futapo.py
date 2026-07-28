"""futapo.com / futapo2.com extractor.

Verified against a real fetch of https://futapo.com/animation-menu/.
Confirmed from the real page:
  - Individual video posts are single root-level slugs, e.g.
    /the-good-stuff-4/, /redapple2-futanari-animation-part-1/.
  - Listing/nav links live under /tag/, /tags/, and self-referential
    "-menu" section links (e.g. /animation-menu/ itself, /futa-comics-menu/
    style section pages), which we exclude.
  - No pagination markup (no rel=next, no /page/N/, no numbered links, no
    load-more/AJAX hints) was found on this listing, so it appears to be a
    single, non-paginated page. common.py's generic fallback will still
    try guessing a /2/ page defensively, but gracefully stops (rather than
    erroring) if that guess 404s, so this is safe either way.

futapo2.com is the same template/site family (confirmed: futapo.com listing
pages link out to individual futapo2.com video posts, and futapo2.com serves
creator gallery pages like /radroachhd/ with the identical single-root-slug
post pattern), so it's included here rather than as a separate extractor.
"""

from __future__ import annotations

from veloci_engine.extractors.common import GenericListingExtractor

_EXCLUDED_PREFIXES = (
    "/category/",
    "/tag/",
    "/tags/",
    "/page/",
    "/author/",
    "/wp-",
    "/feed",
    "/search",
)

VIDEO_URL_PATTERN = (
    r"^/(?!" + "|".join(p.lstrip("/") for p in _EXCLUDED_PREFIXES) + r")"
    r"(?!.*-menu/?$)[^/]+/?$"
)

EXTRACTOR = GenericListingExtractor(
    name="futapo",
    domains={"futapo.com", "futapo2.com"},
    video_url_pattern=VIDEO_URL_PATTERN,
)
