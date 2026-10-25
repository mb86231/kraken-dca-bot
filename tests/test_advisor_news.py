"""Tests for the crypto news fetcher (advisory-only briefing blocks).

The bot fetches RSS/Atom headlines itself; failures degrade to empty
lists and never raise into the briefing.
"""

from __future__ import annotations

from bot.advisor.news import (
    Headline,
    NewsFetcher,
    base_asset,
    filter_pair,
    pair_keywords,
    parse_feed,
)

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>CoinDesk</title>
    <item>
      <title>Bitcoin ETF inflows hit monthly high</title>
      <link>https://example.test/1</link>
      <pubDate>Thu, 09 Oct 2026 08:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Ethereum upgrade goes live on testnet</title>
      <link>https://example.test/2</link>
    </item>
    <item>
      <title>&lt;b&gt;Regulators&lt;/b&gt; weigh new stablecoin rules</title>
    </item>
  </channel>
</rss>
"""

ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <entry>
    <title>Solana network outage resolved</title>
    <link href="https://example.test/3"/>
  </entry>
  <entry>
    <title>Macro outlook: rates on hold</title>
  </entry>
</feed>
"""


def test_base_asset_common_pairs():
    assert base_asset("XBTCHF") == "XBT"
    assert base_asset("ETHEUR") == "ETH"
    assert base_asset("SOLUSD") == "SOL"
    assert base_asset("") == ""


def test_pair_keywords_known_and_fallback():
    assert "bitcoin" in pair_keywords("XBTCHF")
    assert "ethereum" in pair_keywords("ETHEUR")
    # Unknown asset: falls back to the lowercase ticker itself.
    assert pair_keywords("PEPEEUR") == ("pepe",)


def test_filter_pair_case_insensitive():
    headlines = [
        Headline(title="Bitcoin ETF inflows rise"),
        Headline(title="ETHEREUM upgrade ships"),
        Headline(title="Traditional markets rally"),
    ]
    assert [h.title for h in filter_pair(headlines, "XBTCHF")] == [
        "Bitcoin ETF inflows rise"
    ]
    assert [h.title for h in filter_pair(headlines, "ETHEUR")] == [
        "ETHEREUM upgrade ships"
    ]


def test_parse_feed_rss():
    headlines = parse_feed(RSS_SAMPLE, source="coindesk.test")
    assert len(headlines) == 3
    assert headlines[0].title == "Bitcoin ETF inflows hit monthly high"
    assert headlines[0].source == "coindesk.test"
    assert headlines[0].url == "https://example.test/1"
    # Inline HTML markup is stripped from titles; entities are unescaped.
    assert headlines[2].title == "Regulators weigh new stablecoin rules"


def test_parse_feed_atom():
    headlines = parse_feed(ATOM_SAMPLE, source="atom.test")
    assert len(headlines) == 2
    assert headlines[0].title == "Solana network outage resolved"


def test_parse_feed_garbage_returns_empty():
    assert parse_feed("not xml at all") == []
    assert parse_feed("") == []


class _StubFetcher(NewsFetcher):
    """Returns canned bodies or raises, per URL."""

    def __init__(self, feeds, bodies):
        super().__init__(feeds, cache_seconds=0)
        self._bodies = bodies
        self.calls = 0

    def _get(self, url: str) -> str:
        self.calls += 1
        body = self._bodies.get(url)
        if isinstance(body, Exception):
            raise body
        return body or ""


def test_fetcher_combines_feeds_and_ignores_failures():
    fetcher = _StubFetcher(
        ["https://a.test/rss", "https://b.test/rss"],
        {
            "https://a.test/rss": RSS_SAMPLE,
            "https://b.test/rss": ConnectionError("down"),
        },
    )
    headlines = fetcher.fetch_all()
    # The failing feed is skipped; the working one still delivers.
    assert len(headlines) == 3
    assert all(h.source == "a.test" for h in headlines)


def test_fetcher_caches_within_window():
    fetcher = _StubFetcher(
        ["https://a.test/rss"], {"https://a.test/rss": RSS_SAMPLE}
    )
    fetcher.cache_seconds = 1800
    first = fetcher.fetch_all()
    second = fetcher.fetch_all()
    assert first == second
    assert fetcher.calls == 1


def test_fetcher_no_feeds_configured():
    assert NewsFetcher([]).fetch_all() == []
