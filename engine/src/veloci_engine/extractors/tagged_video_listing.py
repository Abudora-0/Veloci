"""Extractor for a tag/category-browsable tube-site template.

UNVERIFIED against the live site (no network access in this sandbox).
Assumption: video pages live at /video/<id>/<slug>/, which is the
convention on most clones of this tube-site template. Tag/category
listings are assumed to paginate via a trailing /<n>/ segment or an
explicit rel="next" link, both handled generically by common.py.

If real markup differs, only VIDEO_URL_PATTERN (and possibly a custom
next_page function) need to change here.
"""

from __future__ import annotations

from veloci_engine.extractors.common import GenericListingExtractor

VIDEO_URL_PATTERN = r"^/video/\d+/"

EXTRACTOR = GenericListingExtractor(
    name="tagged_video_listing",
    domains={"rule34video.com"},
    video_url_pattern=VIDEO_URL_PATTERN,
)
