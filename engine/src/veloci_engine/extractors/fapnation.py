"""fap-nation.org extractor.

Verified against a real fetch of https://fap-nation.org/category/animation-porn/
(with a full browser User-Agent, since a bare/minimal one gets Cloudflare-403'd).
Confirmed from the real page:
  - Individual video posts are single root-level slugs, e.g.
    /rouges-plan-rouge-the-bat-animation-cartoonsaur/ (WordPress default
    permalink structure), including percent-encoded/unicode titles.
  - Listing/taxonomy links live under /category/, /tag/, /advanced-search,
    which we exclude rather than positively match, since the real slug
    shape has no fixed pattern.
  - Pagination uses <link rel="next" href=".../page/2/">, i.e. WordPress's
    default /page/N/ scheme, matching common.py's default_next_page.
  - The theme (tagDiv Newspaper) renders a category page as several
    side-by-side "blocks": a "POPULAR ANIMATION" widget (top-7-days,
    fixed limit) and a cross-category promo (e.g. "HENTAI") alongside the
    real "LATEST ANIMATION" post grid -- confirmed on
    /category/animation-porn/page/2/, where Popular+Hentai contributed 12
    of 35 raw candidates, all identical to what page/3/ also shows, since
    "popular this week" and the promo widget don't change per page.
    Scoped extraction to just the "LATEST ..." block's container.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser, Node

from veloci_engine.extractors.common import GenericListingExtractor

_LATEST_BLOCK_MARKER = "latest"


def _latest_block_anchors(tree: HTMLParser) -> list[Node] | None:
    for heading in tree.css(".td-block-title-wrap"):
        text = (heading.text() or "").strip().lower()
        if _LATEST_BLOCK_MARKER not in text:
            continue
        container = heading.next
        while container is not None and getattr(container, "tag", None) != "div":
            container = container.next
        if container is not None:
            return container.css("a[href]")
    # No "LATEST ..." block found -- template differs from what we
    # verified (e.g. a non-category listing page), fall back to the
    # whole page rather than yielding nothing.
    return None

_EXCLUDED_PREFIXES = (
    "/category/",
    "/tag/",
    "/tags/",
    "/page/",
    "/author/",
    "/wp-",
    "/feed",
    "/search",
    "/advanced-search",
    "/model/",
    "/models/",
)

# Any single root-level path segment not starting with a known non-video
# prefix. [^/]+ (not a fixed charset) so percent-encoded/unicode slugs match.
VIDEO_URL_PATTERN = r"^/(?!" + "|".join(p.lstrip("/") for p in _EXCLUDED_PREFIXES) + r")[^/]+/?$"

EXTRACTOR = GenericListingExtractor(
    name="fapnation",
    domains={"fap-nation.org"},
    video_url_pattern=VIDEO_URL_PATTERN,
    anchor_scope=_latest_block_anchors,
)
