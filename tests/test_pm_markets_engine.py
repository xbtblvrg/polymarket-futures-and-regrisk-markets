"""Anti-tautological mathematical and architectural invariant tests for `pm_markets_engine`."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from pm_markets_engine import (
    AutonomousMarketsExecutor,
    AutonomousMarketsGuardian,
    CentralWalletGuard,
    LadderLevel,
    MarketStrategyOptimizer,
    MarketVault,
    OrderbookSnapshot,
    OutcomeCandidate,
    TargetLegPlan,
    integrate_ask_ladder,
    integrate_bid_ladder,
)


class TestPmMarketsEngineInvariants(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp_dir.name)
        self.guard = CentralWalletGuard(data_dir=self.data_dir)
        self.executor = AutonomousMarketsExecutor(wallet_guard=self.guard, dry_run=True)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_two_layer_market_isolation_and_maker_collateral_guard(self) -> None:
        """Verifies Section 7.3: Market A and Market B have separate SQLite DBs and cannot spend each other's Maker locks."""
        vault_a = MarketVault("market_alpha", data_dir=self.data_dir)
        vault_b = MarketVault("market_beta", data_dir=self.data_dir)
        self.assertNotEqual(vault_a.db_path, vault_b.db_path)

        # Physical wallet has $1,500 USDC.e
        wallet_usdc = 1500.0
        rem_after_a = self.guard.approve_deposit(vault_a, 1000.0, on_chain_wallet_usdc=wallet_usdc)
        self.assertAlmostEqual(rem_after_a, 500.0, places=4)

        # Attempting to allocate $600 to Market B when only $500 is unallocated on-chain must fail
        with self.assertRaises(ValueError):
            self.guard.approve_deposit(vault_b, 600.0, on_chain_wallet_usdc=wallet_usdc)

        # Allocating $500 to Market B succeeds
        rem_after_b = self.guard.approve_deposit(vault_b, 500.0, on_chain_wallet_usdc=wallet_usdc)
        self.assertAlmostEqual(rem_after_b, 0.0, places=4)

        # Market A places a resting Maker BUY order locking $600 (3000 shares @ 0.20)
        ord_a1 = self.executor.submit_isolated_limit_order(
            vault=vault_a,
            token_id="tok_a_1",
            side="BUY",
            limit_price=0.20,
            size_shares=3000.0,
            on_chain_wallet_usdc=wallet_usdc,
            simulated_order_id="ord_a_1",
        )
        state_a = vault_a.get_cash_state()
        self.assertAlmostEqual(state_a.total_unspent_cash, 1000.0, places=4)
        self.assertAlmostEqual(state_a.open_buy_locks, 600.0, places=4)
        self.assertAlmostEqual(state_a.available_cash, 400.0, places=4)

        # Token ownership lock: Market B cannot trade or claim `tok_a_1`
        with self.assertRaises(PermissionError):
            self.executor.submit_isolated_limit_order(
                vault=vault_b,
                token_id="tok_a_1",
                side="BUY",
                limit_price=0.20,
                size_shares=100.0,
                on_chain_wallet_usdc=wallet_usdc,
                simulated_order_id="ord_b_illegal",
            )

        # Cross-market cancellation lock: Market B cannot cancel Market A's order
        with self.assertRaises(PermissionError):
            self.executor.cancel_market_order_by_id(vault_b, ord_a1)

        # Code-level ban on global cancel_all() and cancel_market_orders()
        with self.assertRaises(RuntimeError):
            self.executor.cancel_all()
        with self.assertRaises(RuntimeError):
            self.executor.cancel_market_orders()

    def test_orderbook_integrals_and_reality_first_go_vs_no_go(self) -> None:
        """Verifies Section 1.1.2, 3.2, and 3.8.1: discounted early-phase market is GO; compressed 0.96 market is NO-GO."""
        deep_book_a = OrderbookSnapshot(
            token_id="t1",
            bids=(LadderLevel(0.19, 5000.0), LadderLevel(0.18, 5000.0)),
            asks=(LadderLevel(0.20, 5000.0),),
            tick_size=0.001,
            min_order_size=5.0,
        )
        deep_book_b = OrderbookSnapshot(
            token_id="t2",
            bids=(LadderLevel(0.19, 5000.0), LadderLevel(0.18, 5000.0)),
            asks=(LadderLevel(0.20, 5000.0),),
            tick_size=0.001,
            min_order_size=5.0,
        )
        base_book = OrderbookSnapshot(
            token_id="t3",
            bids=(LadderLevel(0.27, 5000.0), LadderLevel(0.26, 5000.0)),
            asks=(LadderLevel(0.28, 5000.0),),
            tick_size=0.001,
            min_order_size=5.0,
        )

        # Case 1: Early-phase market with 0.68 total entry sum (0.40 alpha + 0.28 base) and 0.92 true prob coverage
        early_candidates = [
            OutcomeCandidate("t1", "Alpha_1", true_prob=0.30, entry_price=0.20, role_set="S_alpha", orderbook=deep_book_a),
            OutcomeCandidate("t2", "Alpha_2", true_prob=0.30, entry_price=0.20, role_set="S_alpha", orderbook=deep_book_b),
            OutcomeCandidate("t3", "Base_1", true_prob=0.32, entry_price=0.28, role_set="S_base", orderbook=base_book),
        ]
        plan_go = MarketStrategyOptimizer.build_graded_asymmetric_plan(
            candidates=early_candidates,
            budget_usd=1000.0,
            base_return_floor=0.15,
            min_required_rr=1.5,
        )
        self.assertTrue(plan_go.is_viable_go)
        self.assertGreater(plan_go.q_alpha, plan_go.q_base)
        self.assertGreater(plan_go.q_base, 1000.0)
        self.assertGreater(plan_go.effective_rr, 2.0)

        # Case 2: Compressed late-stage market with 0.96 entry sum -> must return NO-GO
        compressed_candidates = [
            OutcomeCandidate("t1", "Comp_1", true_prob=0.32, entry_price=0.34, role_set="S_alpha", orderbook=deep_book_a),
            OutcomeCandidate("t2", "Comp_2", true_prob=0.32, entry_price=0.34, role_set="S_alpha", orderbook=deep_book_b),
            OutcomeCandidate("t3", "Comp_Base", true_prob=0.28, entry_price=0.28, role_set="S_base", orderbook=base_book),
        ]
        plan_nogo = MarketStrategyOptimizer.build_graded_asymmetric_plan(
            candidates=compressed_candidates,
            budget_usd=1000.0,
            base_return_floor=0.10,
            min_required_rr=1.0,
        )
        self.assertFalse(plan_nogo.is_viable_go)

        # Verify Ask/Bid ladder integration math
        ask_int = integrate_ask_ladder(
            (LadderLevel(0.10, 100.0), LadderLevel(0.12, 100.0)),
            target_shares=150.0,
        )
        self.assertAlmostEqual(ask_int.total_usdc, 100.0 * 0.10 + 50.0 * 0.12, places=6)
        self.assertTrue(ask_int.fully_absorbable)

        bid_int = integrate_bid_ladder(
            (LadderLevel(0.15, 80.0), LadderLevel(0.12, 100.0)),
            held_shares=130.0,
        )
        self.assertAlmostEqual(bid_int.total_usdc, 80.0 * 0.15 + 50.0 * 0.12, places=6)

    def test_three_way_reconciliation_and_guardian(self) -> None:
        """Verifies Section 7.1 & 7.2: 3-Way Truth Reconciliation and Auto-Pegging."""
        vault = MarketVault("test_reconcile", data_dir=self.data_dir)
        self.guard.approve_deposit(vault, 500.0, on_chain_wallet_usdc=1000.0)
        vault.upsert_target_leg(
            TargetLegPlan(
                token_id="tok_x",
                outcome_name="Contender_X",
                role_set="S_alpha",
                target_shares=1000.0,
                max_entry_price=0.25,
                tick_size=0.001,
                min_order_size=5.0,
            )
        )
        oid = self.executor.submit_isolated_limit_order(
            vault=vault,
            token_id="tok_x",
            side="BUY",
            limit_price=0.24,
            size_shares=1000.0,
            on_chain_wallet_usdc=1000.0,
            simulated_order_id="ord_x_1",
        )
        # Partial fill: 600 shares settled in wallet, 400 still working
        vault.record_order_fill(oid, newly_matched_shares=600.0, fill_price=0.24)
        rows_partial = self.executor.reconcile_market_state(vault, wallet_shares_override={"tok_x": 600.0})
        self.assertEqual(len(rows_partial), 1)
        self.assertFalse(rows_partial[0].is_verified_on_chain)
        self.assertAlmostEqual(rows_partial[0].unfilled_discrepancy, 400.0, places=4)
        self.assertAlmostEqual(rows_partial[0].open_order_shares, 400.0, places=4)

        # Complete fill: remaining 400 shares settled in wallet
        vault.record_order_fill(oid, newly_matched_shares=400.0, fill_price=0.24)
        rows_done = self.executor.reconcile_market_state(vault, wallet_shares_override={"tok_x": 1000.0})
        self.assertTrue(rows_done[0].is_verified_on_chain)
        self.assertEqual(rows_done[0].status_action, "VERIFIED_ON_CHAIN")

        receipt_md = self.executor.format_three_column_receipt(rows_done)
        self.assertIn("VERIFIED_ON_CHAIN", receipt_md)

        guardian = AutonomousMarketsGuardian(self.guard, self.executor)
        live_book = OrderbookSnapshot(
            token_id="tok_x",
            bids=(LadderLevel(0.245, 500.0),),
            asks=(LadderLevel(0.260, 500.0),),
            tick_size=0.001,
            min_order_size=5.0,
        )
        new_peg = guardian.evaluate_auto_peg_price(
            current_limit_price=0.240,
            max_entry_price=0.250,
            live_book=live_book,
        )
        self.assertAlmostEqual(new_peg or 0.0, 0.246, places=6)


if __name__ == "__main__":
    unittest.main()
