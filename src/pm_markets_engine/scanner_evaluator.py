"""Live CLOB Orderbook Integral & Market Evaluator (`MarketScannerEvaluator`).

Implements Section 3.8.1, Section 3.8.2, and Section 6.1 of the Master Blueprint:
- Exact Ask-ladder integration for entry slippage and VWAP.
- Exact Bid-ladder integration (`\int_0^{q_i} Bid_i(u) du`) for true endogenous liquidation value (`MaxLoss_guarded`).
- Dynamic tick size (`orderPriceMinTickSize`) and minimum order size (`min_order_size`) extraction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple
import requests


GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"


@dataclass(frozen=True)
class LadderLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderbookSnapshot:
    token_id: str
    bids: Tuple[LadderLevel, ...]
    asks: Tuple[LadderLevel, ...]
    tick_size: float
    min_order_size: float

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def spread(self) -> float:
        if not self.bids or not self.asks:
            return 1.0
        return round(max(0.0, self.best_ask - self.best_bid), 6)


@dataclass(frozen=True)
class BookIntegralResult:
    target_shares: float
    filled_shares: float
    unfilled_shares: float
    total_usdc: float
    vwap_price: float
    worst_fill_price: float
    fully_absorbable: bool


def integrate_ask_ladder(asks: Tuple[LadderLevel, ...], target_shares: float) -> BookIntegralResult:
    """Computes exact entry cost integral across ascending Ask levels."""
    if target_shares <= 0.0:
        return BookIntegralResult(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True)
    sorted_asks = sorted(asks, key=lambda lvl: lvl.price)
    rem = target_shares
    cost = 0.0
    filled = 0.0
    worst_p = 0.0
    for lvl in sorted_asks:
        if rem <= 1e-9:
            break
        take = min(rem, lvl.size)
        if take > 0:
            cost += take * lvl.price
            filled += take
            rem -= take
            worst_p = lvl.price
    vwap = (cost / filled) if filled > 0 else 0.0
    return BookIntegralResult(
        target_shares=round(target_shares, 6),
        filled_shares=round(filled, 6),
        unfilled_shares=round(max(0.0, rem), 6),
        total_usdc=round(cost, 6),
        vwap_price=round(vwap, 6),
        worst_fill_price=round(worst_p, 6),
        fully_absorbable=(rem <= 1e-6),
    )


def integrate_bid_ladder(bids: Tuple[LadderLevel, ...], held_shares: float) -> BookIntegralResult:
    """Computes exact endogenous liquidation value (`\int_0^{q_i} Bid_i(u) du`) across descending Bid levels."""
    if held_shares <= 0.0:
        return BookIntegralResult(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True)
    sorted_bids = sorted(bids, key=lambda lvl: lvl.price, reverse=True)
    rem = held_shares
    proceeds = 0.0
    filled = 0.0
    worst_p = 0.0
    for lvl in sorted_bids:
        if rem <= 1e-9:
            break
        sell = min(rem, lvl.size)
        if sell > 0:
            proceeds += sell * lvl.price
            filled += sell
            rem -= sell
            worst_p = lvl.price
    vwap = (proceeds / filled) if filled > 0 else 0.0
    return BookIntegralResult(
        target_shares=round(held_shares, 6),
        filled_shares=round(filled, 6),
        unfilled_shares=round(max(0.0, rem), 6),
        total_usdc=round(proceeds, 6),
        vwap_price=round(vwap, 6),
        worst_fill_price=round(worst_p, 6),
        fully_absorbable=(rem <= 1e-6),
    )


class MarketScannerEvaluator:
    """Fetches untruncated Gamma event metadata and live CLOB orderbooks."""

    def __init__(self, timeout_sec: float = 15.0) -> None:
        self.timeout_sec = timeout_sec

    def fetch_event_by_slug(self, event_slug: str) -> Dict[str, Any]:
        resp = requests.get(
            f"{GAMMA_API_BASE}/events",
            params={"slug": event_slug},
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        items = resp.json()
        if not items:
            raise LookupError(f"Event slug {event_slug!r} not found on Gamma API.")
        return items[0]

    def fetch_orderbook(self, token_id: str, fallback_tick: float = 0.001, fallback_min_size: float = 5.0) -> OrderbookSnapshot:
        resp = requests.get(
            f"{CLOB_API_BASE}/book",
            params={"token_id": token_id},
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        raw = resp.json()
        bids = tuple(
            sorted(
                (LadderLevel(float(b["price"]), float(b["size"])) for b in raw.get("bids", [])),
                key=lambda x: x.price,
                reverse=True,
            )
        )
        asks = tuple(
            sorted(
                (LadderLevel(float(a["price"]), float(a["size"])) for a in raw.get("asks", [])),
                key=lambda x: x.price,
            )
        )
        tick = float(raw.get("tick_size") or fallback_tick)
        min_sz = float(raw.get("min_order_size") or fallback_min_size)
        return OrderbookSnapshot(
            token_id=token_id,
            bids=bids,
            asks=asks,
            tick_size=tick,
            min_order_size=min_sz,
        )
