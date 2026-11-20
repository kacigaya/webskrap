"""Supported search engines."""

from __future__ import annotations

from enum import StrEnum


class SearchEngine(StrEnum):
    """Which search engine's results page a search loads.

    ``BING`` is the default because DuckDuckGo frequently serves bot challenges.
    ``DDG`` selects DuckDuckGo's no-JavaScript HTML endpoint. Google is absent
    on purpose: from a fresh headless session it serves a consent wall or a CAPTCHA, and its
    markup changes too often to keep an extractor honest.
    """

    DDG = "ddg"
    BING = "bing"
