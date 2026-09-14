"""Shared scraping helpers used by the individual site extractors.

None of the markup assumptions here have been checked against the live
sites (this sandbox has no network access to them), so each extractor built
on top of this is a best-effort guess based on common tube-site/WordPress
template conventions, and is expected to need a fix-up pass once run
against the real pages. See each extractor module's module docstring for
what specifically is unverified.
"""

from __future__ import annotations

import re
import time
from typing import Callable, Iterator
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from veloci_engine.extractors.base import ExtractorError

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Between paginated requests, so a crawl doesn't hammer the site.
REQUEST_DELAY_SECONDS = 1.0


def _cloudflare_challenge_notice(response: httpx.Response) -> str | None:
    """If this response is a Cloudflare bot challenge, describe it plainly.

    Sites occasionally turn on Cloudflare's managed/JS challenge after an
    extractor was written and verified against them (confirmed happening to
    one of the supported sites). That's not a parsing bug and not something
    a plain HTTP request can solve -- it needs a real browser -- so it
    deserves a message that says so instead of surfacing as a bare
    "403 Forbidden".
    """
    if "cloudflare" not in response.headers.get("server", "").lower():
        return None
    if response.headers.get("cf-mitigated", "").lower() == "challenge" or (
        response.status_code in (403, 503) and "challenges.cloudflare.com" in response.text
    ):
        return (
            "blocked by a Cloudflare bot challenge -- this site now requires "
            "passing a browser challenge that a plain request can't solve. "
            "Try again later, or check whether the page loads in a normal "
            "browser."
        )
    return None


def fetch(url: str, *, client: httpx.Client | None = None) -> str:
    owns_client = client is None
    client = client or httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=20.0)
    try:
        response = client.get(url)
        response.raise_for_status()
        return response.text
    except httpx.HTTPStatusError as exc:
        notice = _cloudflare_challenge_notice(exc.response)
        if notice:
            raise ExtractorError(f"{url}: {notice}") from exc
        raise ExtractorError(f"failed to fetch {url}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ExtractorError(f"failed to fetch {url}: {exc}") from exc
    finally:
        if owns_client:
            client.close()


def default_next_page(tree: HTMLParser, current_url: str) -> str | None:
    """Look for an explicit next-page link before falling back to URL guessing.

    Note: selectolax's CSS engine doesn't support :contains() -- it segfaults
    the process (a native crash, unlike an invalid-selector exception) rather
    than raising, so text-based matching is done in Python instead.
    """
    for selector in ("a[rel='next']", "a.next", ".pagination a.next"):
        node = tree.css_first(selector)
        if node is not None:
            href = node.attributes.get("href")
            if href:
                return urljoin(current_url, href)

    for anchor in tree.css("a[href]"):
        text = (anchor.text() or "").strip().lower()
        if text in ("next", "next »", "»", ">"):
            href = anchor.attributes.get("href")
            if href:
                return urljoin(current_url, href)

    return _increment_trailing_page_number(current_url)


# Some WordPress video-gallery plugins (e.g. "KGVID") embed each video as
# schema.org VideoObject markup rather than linking to a separate post page --
# confirmed on a real creator gallery page, where 36 videos live entirely as
# HTML-entity-escaped itemprop="contentUrl" text (apparently duplicated into a
# meta description for SEO) with a direct .mp4 URL each, no <a href> at all.
# Both the escaped and literal attribute forms are checked since it's not
# guaranteed every theme escapes it the same way.
_EMBEDDED_CONTENT_URL_RES = (
    re.compile(r'itemprop=&quot;contentUrl&quot; content=&quot;([^&"]+?)&quot;'),
    re.compile(r'itemprop="contentUrl" content="([^">]+?)"'),
)
_VIDEO_FILE_EXTENSIONS = (".mp4", ".webm", ".m4v", ".mov")


def _extract_embedded_media_urls(html: str, page_url: str, domains: frozenset[str]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for pattern in _EMBEDDED_CONTENT_URL_RES:
        for match in pattern.finditer(html):
            candidate = urljoin(page_url, match.group(1))
            if candidate in seen:
                continue
            parsed = urlparse(candidate)
            if parsed.netloc.lower().removeprefix("www.") not in domains:
                continue
            if not parsed.path.lower().endswith(_VIDEO_FILE_EXTENSIONS):
                continue
            seen.add(candidate)
            urls.append(candidate)
    return urls


def _increment_trailing_page_number(current_url: str) -> str | None:
    """Fallback: many tube/WordPress templates paginate as .../<n>/ or .../page/<n>/."""
    parsed = urlparse(current_url)
    path = parsed.path.rstrip("/")

    match = re.search(r"/page/(\d+)$", path)
    if match:
        next_n = int(match.group(1)) + 1
        new_path = path[: match.start()] + f"/page/{next_n}/"
        return parsed._replace(path=new_path).geturl()

    match = re.search(r"/(\d+)$", path)
    if match:
        next_n = int(match.group(1)) + 1
        new_path = path[: match.start()] + f"/{next_n}/"
        return parsed._replace(path=new_path).geturl()

    # No page segment yet: this is page 1, try appending /2/.
    return parsed._replace(path=path + "/2/").geturl()


class GenericListingExtractor:
    """Config-driven Extractor: match a video-URL pattern, paginate generically.

    video_url_pattern is matched against the *path* of every <a href> found
    on the listing page (selectolax over the raw HTML, no JS rendering).
    """

    def __init__(
        self,
        name: str,
        domains: set[str] | None,
        video_url_pattern: str,
        *,
        next_page: Callable[[HTMLParser, str], str | None] = default_next_page,
        # Scan exactly the page the user gives us, no more. A category/tag
        # listing (e.g. a site's /category/<name>/page/2/) has
        # its own rel="next" link chasing forward through the *entire*
        # site's catalog (confirmed: that category has 529 pages) -- with a
        # higher cap here, scanning "page 2" silently keeps crawling into
        # page 3, 4, 5... and the user ends up with hundreds of unrelated
        # videos they never asked for. If they want another page, they scan
        # its URL directly, same as they already do to reach page 2.
        max_pages: int = 1,
        # Narrows which anchors on the page count as video candidates at
        # all, e.g. to scope out a "Popular"/cross-category-promo widget
        # that repeats the same handful of videos on every page. Returns
        # None to mean "no scoping needed, use the whole page" (the
        # default: every anchor in the document).
        anchor_scope: Callable[[HTMLParser], list | None] | None = None,
    ) -> None:
        self.name = name
        # None means "no fixed domain list" -- used by the generic fallback
        # extractor (extractors/fallback.py) for sites with no dedicated
        # extractor: it scopes itself to whatever host the listing URL
        # actually is at crawl time (see iter_video_urls below) instead of a
        # pre-registered set, and matches() always succeeds so it can be
        # tried as a last resort for any domain.
        self.domains = frozenset(domains) if domains is not None else None
        self._video_re = re.compile(video_url_pattern)
        self._next_page = next_page
        self._max_pages = max_pages
        self._anchor_scope = anchor_scope

    def matches(self, url: str) -> bool:
        if self.domains is None:
            return True
        return urlparse(url).netloc.lower().removeprefix("www.") in self.domains

    def iter_video_urls(self, listing_url: str, *, max_items: int | None = None) -> Iterator[str]:
        seen: set[str] = set()
        yielded = 0
        url: str | None = listing_url
        # A fixed extractor already knows its domain(s) up front; the
        # generic fallback (self.domains is None) instead scopes itself to
        # whichever host the caller actually pasted, computed once here so
        # every page of this same crawl stays scoped to that one site
        # rather than drifting if a redirect changes host mid-crawl.
        allowed_domains = (
            self.domains
            if self.domains is not None
            else frozenset({urlparse(listing_url).netloc.lower().removeprefix("www.")})
        )
        client = httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=20.0)
        try:
            for page_num in range(self._max_pages):
                if url is None or (max_items is not None and yielded >= max_items):
                    return
                try:
                    html = fetch(url, client=client)
                except ExtractorError:
                    # First page failing is a real error (bad listing URL); a
                    # later page failing usually just means our guessed/found
                    # "next" URL doesn't exist, i.e. we've reached the end.
                    if page_num == 0:
                        raise
                    return
                tree = HTMLParser(html)

                # Embedded schema.org media (direct file URLs, no separate post
                # page) is structured data, not a link-shape guess -- trust it
                # unconditionally rather than running it through the
                # occurrence-threshold heuristic below.
                page_had_new = False
                for embedded_url in _extract_embedded_media_urls(html, url, allowed_domains):
                    if embedded_url in seen:
                        continue
                    seen.add(embedded_url)
                    page_had_new = True
                    yield embedded_url
                    yielded += 1
                    if max_items is not None and yielded >= max_items:
                        return

                # Two passes: real video posts are conventionally linked twice
                # on a listing page (thumbnail + title), while one-off nav/genre
                # links appear once. Tally first, then only treat >=2-occurrence
                # links as videos -- falls back to >=1 if nothing clears that bar,
                # so sites that only link once per item still work.
                anchors = tree.css("a[href]")
                if self._anchor_scope is not None:
                    scoped = self._anchor_scope(tree)
                    if scoped is not None:
                        anchors = scoped

                candidates: list[str] = []
                for anchor in anchors:
                    href = anchor.attributes.get("href")
                    if not href:
                        continue
                    absolute = urljoin(url, href)
                    parsed = urlparse(absolute)
                    host = parsed.netloc.lower().removeprefix("www.")
                    if host not in allowed_domains:
                        continue  # skip ads/affiliates/mirrors on other domains
                    if not self._video_re.search(parsed.path):
                        continue
                    candidates.append(absolute)

                counts: dict[str, int] = {}
                for href in candidates:
                    counts[href] = counts.get(href, 0) + 1
                threshold = 2 if any(c >= 2 for c in counts.values()) else 1

                page_seen: set[str] = set()
                for href in candidates:
                    if href in page_seen or counts[href] < threshold:
                        continue
                    page_seen.add(href)
                    if href in seen:
                        continue
                    seen.add(href)
                    page_had_new = True
                    yield href
                    yielded += 1
                    if max_items is not None and yielded >= max_items:
                        return

                if not page_had_new:
                    return

                next_url = self._next_page(tree, url)
                if next_url == url:
                    return
                url = next_url
                time.sleep(REQUEST_DELAY_SECONDS)
        finally:
            client.close()
