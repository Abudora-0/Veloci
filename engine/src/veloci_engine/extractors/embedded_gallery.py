"""Extractor for a pair of sibling sites sharing one WordPress-adjacent
template, where a listing page's real content lives as schema.org video
markup embedded directly in the page rather than as a separate post per
video (see common.py's _extract_embedded_media_urls).

Verified against a real fetch of one site's main section-listing page.
Confirmed from the real page:
  - Individual video posts are single root-level slugs, e.g.
    /some-clip-title/, /another-clip-title-part-1/.
  - Listing/nav links live under /tag/, /tags/, and self-referential
    "-menu" section links (e.g. the section page itself, or other
    "-menu"-suffixed section pages), which we exclude.
  - No pagination markup (no rel=next, no /page/N/, no numbered links, no
    load-more/AJAX hints) was found on this listing, so it appears to be a
    single, non-paginated page. common.py's own page-number-guessing
    fallback will still try guessing a /2/ page defensively, but
    gracefully stops (rather than erroring) if that guess 404s, so this
    is safe either way.

The second domain is the same template/site family (confirmed: the first
site's listing pages link out to individual video posts on the second
domain, and the second domain also serves creator gallery pages with the
identical single-root-slug post pattern, and the identical embedded-
schema.org-markup structure), so it's included here rather than as a
separate extractor.
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
    name="embedded_gallery",
    domains={"futapo.com", "futapo2.com"},
    video_url_pattern=VIDEO_URL_PATTERN,
)
