"""Domain -> extractor lookup.

New site plugins register themselves here; nothing else in the codebase
needs to change to add a site.
"""

from __future__ import annotations

from veloci_engine.extractors.base import Extractor

_REGISTRY: list[Extractor] = []


def register(extractor: Extractor) -> None:
    _REGISTRY.append(extractor)


def find_extractor(url: str) -> Extractor | None:
    # matches() alone is authoritative -- GenericListingExtractor's own
    # implementation already checks `host in domains` for a fixed-domain
    # extractor and returns True unconditionally for the domain-agnostic
    # fallback (domains=None), so there's nothing to add here.
    for extractor in _REGISTRY:
        if extractor.matches(url):
            return extractor
    return None


def _load_builtin_extractors() -> None:
    from veloci_engine.extractors import category_listing, embedded_gallery, fallback, tagged_video_listing

    # Order matters: find_extractor() returns the first match, so the tuned,
    # site-specific extractors must all be tried before the domain-agnostic
    # fallback (which matches every URL) gets a chance.
    for module in (tagged_video_listing, category_listing, embedded_gallery, fallback):
        register(module.EXTRACTOR)


_load_builtin_extractors()
