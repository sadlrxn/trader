"""Keyword-based news sentiment analysis -- no LLM required.

Ported from IBGA core/shared/sentiment.py. Provides headline-level keyword
matching for bullish/bearish classification. Not wired into the runtime yet;
available for integration when a news data source is added.
"""

from __future__ import annotations

import re

SENTIMENT_KEYWORD_SATURATION = 3  # 3+ keyword matches = max confidence

BULLISH_KEYWORDS: frozenset[str] = frozenset({
    # Earnings & Revenue
    "beat", "beats", "exceeded", "surpassed", "record revenue",
    "raised guidance", "above expectations", "blowout", "earnings surprise",
    "double beat", "record profit", "record quarter",
    # Upgrades & Analyst Actions
    "upgrade", "upgraded", "price target raised", "buy rating",
    "outperform", "overweight", "strong buy", "top pick",
    # Growth & Expansion
    "acquisition", "merger", "partnership", "contract win",
    "new product", "product launch", "expansion",
    # Regulatory & FDA
    "fda approval", "fda approved", "breakthrough designation",
    "positive trial", "trial success", "patent granted",
    # Financial Health
    "dividend increase", "buyback", "share repurchase",
    "margin expansion", "profitability",
    # Market & Momentum
    "rally", "surge", "breakout", "all-time high", "52-week high",
    "short squeeze", "momentum", "insider buying",
    # Macro Positive
    "rate cut", "fed dovish", "soft landing",
})

BEARISH_KEYWORDS: frozenset[str] = frozenset({
    # Earnings & Revenue
    "miss", "misses", "missed", "below expectations", "disappoints",
    "lowered guidance", "cuts guidance", "revenue decline",
    "profit warning", "weak guidance", "negative surprise",
    # Downgrades & Analyst Actions
    "downgrade", "downgraded", "price target cut", "sell rating",
    "underperform", "underweight",
    # Problems & Legal
    "lawsuit", "investigation", "sec investigation", "recall",
    "data breach", "fraud", "class action", "settlement",
    # Regulatory & FDA
    "fda rejection", "fda rejected", "trial failure",
    "regulatory setback", "patent expiration",
    # Financial Distress
    "bankruptcy", "chapter 11", "default", "layoffs",
    "dilution", "secondary offering", "going concern",
    "dividend cut", "margin compression",
    # Market & Momentum
    "crash", "plunge", "selloff", "sell-off", "bear market",
    "capitulation", "insider selling", "death cross", "52-week low",
    # Macro Negative
    "rate hike", "fed hawkish", "recession", "tariff", "tariffs",
    "trade war", "sanctions",
})


def analyze_headline(
    title: str,
    extra_bull: set[str] | None = None,
    extra_bear: set[str] | None = None,
) -> tuple[str, float]:
    """Analyze a single headline via keyword matching.

    Args:
        title: Headline text to analyze.
        extra_bull: Additional bullish keywords.
        extra_bear: Additional bearish keywords.

    Returns:
        Tuple of (sentiment, confidence) where sentiment is one of
        "bullish", "bearish", or "neutral" and confidence is 0.0-1.0.
    """

    lower = title.lower()
    all_bull = BULLISH_KEYWORDS | (extra_bull or set())
    all_bear = BEARISH_KEYWORDS | (extra_bear or set())
    bull_count = sum(1 for kw in all_bull if re.search(rf"\b{re.escape(kw)}\b", lower))
    bear_count = sum(1 for kw in all_bear if re.search(rf"\b{re.escape(kw)}\b", lower))

    if bull_count > bear_count:
        confidence = min(bull_count / SENTIMENT_KEYWORD_SATURATION, 1.0)
        return "bullish", round(confidence, 2)
    if bear_count > bull_count:
        confidence = min(bear_count / SENTIMENT_KEYWORD_SATURATION, 1.0)
        return "bearish", round(confidence, 2)
    return "neutral", 0.1
