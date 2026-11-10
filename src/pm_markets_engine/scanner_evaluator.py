r"""Live CLOB Orderbook Integral & Market Evaluator (`MarketScannerEvaluator`).

Implements Section 3 and Section 5 of the Master Blueprint (`AGENTS.md`):
- Exact Ask-ladder integration for entry slippage and VWAP (`\int_0^{q_i} Ask_i(u) du`).
- Exact Bid-ladder integration (`\int_0^{q_i} Bid_i(u) du`) for true endogenous liquidation value (`MaxLoss_guarded`).
- Dynamic tick size (`orderPriceMinTickSize`) and minimum order size (`min_order_size`) extraction with zero hardcoded fallbacks.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Dict, List, Optional, Tuple
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
    r"""Computes exact endogenous liquidation value (`\int_0^{q_i} Bid_i(u) du`) across descending Bid levels."""
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

    def discover_active_events(self, page_limit: int = 100, max_pages: int = 20) -> List[Dict[str, Any]]:
        """Fetches active Polymarket events with pagination so market discovery is never truncated at page 1."""
        all_events: List[Dict[str, Any]] = []
        for page_idx in range(max_pages):
            offset = page_idx * page_limit
            resp = requests.get(
                f"{GAMMA_API_BASE}/events",
                params={"active": "true", "closed": "false", "limit": page_limit, "offset": offset},
                timeout=self.timeout_sec,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not isinstance(batch, list) or not batch:
                break
            all_events.extend(batch)
            if len(batch) < page_limit:
                break
        return all_events

    @staticmethod
    def extract_event_tokens(event_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extracts ALL sub-markets and BOTH YES and NO token IDs without truncation or YES-only bias."""
        extracted: List[Dict[str, Any]] = []
        for mkt in event_payload.get("markets", []) or []:
            if mkt.get("closed") is True or mkt.get("active") is False:
                continue
            raw_tokens = mkt.get("clobTokenIds") or "[]"
            token_ids = json.loads(raw_tokens) if isinstance(raw_tokens, str) else list(raw_tokens)
            raw_outcomes = mkt.get("outcomes") or '["Yes", "No"]'
            outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else list(raw_outcomes)
            tick = mkt.get("orderPriceMinTickSize") or mkt.get("minimum_tick_size")
            min_sz = mkt.get("orderMinSize") or mkt.get("minimum_order_size")
            for idx, tid in enumerate(token_ids):
                side_label = outcomes[idx] if idx < len(outcomes) else f"Outcome_{idx}"
                extracted.append(
                    {
                        "market_id": str(mkt.get("id", "")),
                        "question": str(mkt.get("question", "")),
                        "group_item_title": str(mkt.get("groupItemTitle") or mkt.get("question") or ""),
                        "outcome_side": str(side_label),
                        "token_id": str(tid),
                        "tick_size": float(tick) if tick is not None else None,
                        "min_order_size": float(min_sz) if min_sz is not None else None,
                    }
                )
        return extracted

    def fetch_orderbook(
        self,
        token_id: str,
        explicit_tick: Optional[float] = None,
        explicit_min_size: Optional[float] = None,
    ) -> OrderbookSnapshot:
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
        raw_tick = raw.get("tick_size") or raw.get("minimum_tick_size") or raw.get("orderPriceMinTickSize") or explicit_tick
        raw_min_sz = raw.get("min_order_size") or raw.get("minimum_order_size") or explicit_min_size
        if raw_tick is None or float(raw_tick) <= 0.0:
            raise ValueError(f"Live CLOB /book response for token_id={token_id!r} missing valid tick_size.")
        if raw_min_sz is None or float(raw_min_sz) < 0.0:
            raise ValueError(f"Live CLOB /book response for token_id={token_id!r} missing valid min_order_size.")
        return OrderbookSnapshot(
            token_id=token_id,
            bids=bids,
            asks=asks,
            tick_size=float(raw_tick),
            min_order_size=float(raw_min_sz),
        )
