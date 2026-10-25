"""Crypto news headlines for the morning briefing (advisory-only).

The bot fetches RSS/Atom headlines itself — the AI provider never goes
to the internet. Headlines render as deterministic briefing blocks and
may be handed to the narrative as context. Any failure (offline feed,
parse error, timeout) degrades to an empty list: news never blocks a
briefing and never raises into the bot.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


class NewsSource(Protocol):
    """Structural type for anything that supplies headlines.

    ``NewsFetcher`` implements it; tests may inject lightweight fakes
    without subclassing.
    """

    def fetch_all(self) -> list[Headline]: ...

# Base asset (Kraken ticker) -> lowercase keywords used to match
# pair-specific news. Unknown assets fall back to the ticker itself.
PAIR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "XBT": ("bitcoin", "btc"),
    "ETH": ("ethereum", "eth"),
    "SOL": ("solana", "sol"),
    "XRP": ("xrp", "ripple"),
    "ADA": ("cardano", "ada"),
    "DOT": ("polkadot", "dot"),
    "LTC": ("litecoin", "ltc"),
    "BCH": ("bitcoin cash", "bch"),
    "LINK": ("chainlink", "link"),
    "AVAX": ("avalanche", "avax"),
    "DOGE": ("dogecoin", "doge"),
    "MATIC": ("polygon", "matic"),
}

_QUOTE_CCYS = ("CHF", "EUR", "USD", "GBP", "JPY", "AUD", "CAD")
_ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass
class Headline:
    title: str
    source: str = ""
    url: str = ""


def base_asset(trading_pair: str) -> str:
    """Base asset of a Kraken pair like 'XBTCHF' -> 'XBT' (best effort)."""
    pair = (trading_pair or "").upper().strip()
    for quote in _QUOTE_CCYS:
        if pair.endswith(quote) and len(pair) > len(quote):
            return pair[: -len(quote)]
    return pair[:-3] if len(pair) > 3 else pair


def pair_keywords(trading_pair: str) -> tuple[str, ...]:
    base = base_asset(trading_pair)
    return PAIR_KEYWORDS.get(base, (base.lower(),))


def filter_pair(headlines: list[Headline], trading_pair: str) -> list[Headline]:
    keywords = pair_keywords(trading_pair)
    return [h for h in headlines if any(k in h.title.lower() for k in keywords)]


def _strip_markup(text: str) -> str:
    """Drop inline HTML tags some feeds leave inside titles."""
    out: list[str] = []
    inside = False
    for ch in text or "":
        if ch == "<":
            inside = True
        elif ch == ">":
            inside = False
        elif not inside:
            out.append(ch)
    return "".join(out).strip()


def _child_text(item: ET.Element, tag: str) -> str:
    node = item.find(tag)
    if node is None or not node.text:
        node = item.find(f"{_ATOM}{tag}")
    return node.text.strip() if node is not None and node.text else ""


def parse_feed(xml_text: str, source: str = "", limit: int = 8) -> list[Headline]:
    """Parse an RSS 2.0 or Atom feed; tolerant of missing fields."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("News feed parse failed (%s): %s", source or "feed", exc)
        return []
    items = root.findall(".//item")
    if not items:
        items = root.findall(f".//{_ATOM}entry")
    headlines: list[Headline] = []
    for item in items[:limit]:
        title = _strip_markup(_child_text(item, "title"))
        if title:
            headlines.append(Headline(title=title, source=source, url=_child_text(item, "link")))
    return headlines


class NewsFetcher:
    """Fetches the configured feeds with a short in-memory cache.

    ``_get`` is a seam for tests: subclass or monkeypatch it to avoid
    real network access.
    """

    def __init__(
        self,
        feeds,
        timeout: float = 8.0,
        cache_seconds: float = 1800.0,
        max_headlines: int = 10,
    ):
        self.feeds = [str(f) for f in feeds if str(f).strip()]
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.max_headlines = max_headlines
        self._cache: tuple[float | None, list[Headline]] = (None, [])

    def _get(self, url: str) -> str:
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, follow_redirects=True)
            response.raise_for_status()
            return response.text

    def fetch_all(self) -> list[Headline]:
        now = time.monotonic()
        stamp, cached = self._cache
        if stamp is not None and now - stamp < self.cache_seconds:
            return list(cached)
        headlines: list[Headline] = []
        for url in self.feeds:
            try:
                body = self._get(url)
            except Exception as exc:
                logger.warning("News feed fetch failed (%s): %s", url, exc)
                continue
            source = url.split("//")[-1].split("/")[0]
            headlines.extend(parse_feed(body, source=source))
        headlines = headlines[: self.max_headlines]
        self._cache = (now, headlines)
        return list(headlines)
