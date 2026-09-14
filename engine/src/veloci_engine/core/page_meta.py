"""Shared post-page scraping: poster image + direct per-quality download
links that yt-dlp's generic <video>/<source> scraper never sees.

Confirmed live on one supported site: the embedded player yt-dlp finds
points at an entirely different, single-fixed-quality mirror, while the
page's own "DOWNLOAD" buttons are plain <a href> links (not
<video>/<source> tags, so yt-dlp's scraper skips them) pointing directly at
a real per-quality CDN -- e.g.
https://vz-8e56367c-501.b-cdn.net/<id>/play_360p.mp4, .../play_480p.mp4,
.../play_720p.mp4 -- confirmed independently downloadable (200, real
Content-Length, no Referer needed). Using these directly means an exact-
quality file with no local re-encode required, versus downloading the
single-fixed-quality mirror's master and re-encoding it down with ffmpeg
(the multi-minute wait users were hitting for anything other than "best").

One HTTP fetch serves both probe.py (thumbnail) and downloader.py (quality
links) -- previously each did its own separate fetch, doubling probe
latency for no reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx
from selectolax.parser import HTMLParser

from veloci_engine.extractors.common import DEFAULT_HEADERS

# Bunny Stream's (and apparently this whole WordPress theme's) convention
# for direct per-quality download buttons. Deliberately not site-scoped --
# any page using this same naming pattern benefits for free.
_QUALITY_LINK_RE = re.compile(r"play_(\d+)p\.mp4", re.IGNORECASE)


@dataclass
class PageMeta:
    thumbnail: str | None = None
    # height -> direct download url, e.g. {360: "https://.../play_360p.mp4"}
    quality_links: dict[int, str] = field(default_factory=dict)


def fetch_page_meta(url: str, *, timeout: float = 8.0) -> PageMeta:
    """Best-effort; a slow/unreachable page just means no thumbnail and no
    direct-quality fast path, never a hard failure for the caller."""
    try:
        with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=timeout) as client:
            response = client.get(url)
            if response.status_code != 200:
                return PageMeta()
            tree = HTMLParser(response.text)

            thumbnail = None
            for selector in ('meta[property="og:image"]', 'meta[name="twitter:image"]'):
                node = tree.css_first(selector)
                if node is None:
                    continue
                content = (node.attributes.get("content") or "").strip()
                if content.startswith("http"):
                    thumbnail = content
                    break

            quality_links: dict[int, str] = {}
            for anchor in tree.css("a[href]"):
                href = anchor.attributes.get("href") or ""
                match = _QUALITY_LINK_RE.search(href)
                if match:
                    quality_links[int(match.group(1))] = href

            return PageMeta(thumbnail=thumbnail, quality_links=quality_links)
    except httpx.HTTPError:
        return PageMeta()
