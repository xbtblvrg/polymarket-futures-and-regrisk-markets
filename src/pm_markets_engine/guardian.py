"""Sequential Multiplexed Background Guardian (`AutonomousMarketsGuardian`).

Implements Section 6.4, Section 7.2.2, and Section 7.3.2.C of the Master Blueprint:
- Single sequential `flock`-synchronized loop across all registered `data/pm_markets/*.sqlite3` databases.
- Runs 3-Way Truth Reconciliation and Auto-Pegging (`Cancel & Replace` within max entry price and tick size).
"""
from __future__ import annotations

from typing import Dict, List

from .market_vault import MarketVault
from .pm_markets_executor import AutonomousMarketsExecutor, LegReconciliationRow
from .scanner_evaluator import OrderbookSnapshot
from .wallet_guard import CentralWalletGuard


class AutonomousMarketsGuardian:
    """24/7 sequential guardian managing open Maker limits and on-chain reconciliation."""

    def __init__(self, wallet_guard: CentralWalletGuard, executor: AutonomousMarketsExecutor) -> None:
        self.wallet_guard = wallet_guard
        self.executor = executor

    def evaluate_auto_peg_price(
        self,
        current_limit_price: float,
        max_entry_price: float,
        live_book: OrderbookSnapshot,
    ) -> float | None:
        """Section 7.2.2: Computes new Maker limit price if queue priority is lost but within `max_entry_price`."""
        if not live_book.bids:
            return None
        best_bid = live_book.best_bid
        tick = live_book.tick_size
        if best_bid <= current_limit_price + 1e-9:
            return None
        candidate_price = round(best_bid + tick, 6)
        if candidate_price >= live_book.best_ask:
            candidate_price = round(best_bid, 6)
        if candidate_price <= max_entry_price + 1e-9 and candidate_price > current_limit_price + 1e-9:
            return candidate_price
        return None

    def run_single_reconciliation_sweep(self) -> Dict[str, List[LegReconciliationRow]]:
        """Sequentially reconciles all registered markets under the global `flock` mutex."""
        results: Dict[str, List[LegReconciliationRow]] = {}
        with self.wallet_guard.execution_lock():
            for vault in self.wallet_guard.list_registered_vaults():
                results[vault.market_slug] = self.executor.reconcile_market_state(vault)
        return results
