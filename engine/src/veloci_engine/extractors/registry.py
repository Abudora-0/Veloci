"""Domain -> extractor lookup.

New site plugins register themselves here; nothing else in the codebase
needs to change to add a site.
"""

from __future__ import annotations

from urllib.parse import urlparse

from veloci_engine.extractors.base import Extractor

_REGISTRY: list[Extractor] = []


def register(extractor: Extractor) -> None:
    _REGISTRY.append(extractor)


def find_extractor(url: str) -> Extractor | None:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for extractor in _REGISTRY:
        if host in extractor.domains or extractor.matches(url):
            return extractor
    return None


def _load_builtin_extractors() -> None:
    from veloci_engine.extractors import fapnation, futapo, rule34video

    for module in (rule34video, fapnation, futapo):
        register(module.EXTRACTOR)


_load_builtin_extractors()
