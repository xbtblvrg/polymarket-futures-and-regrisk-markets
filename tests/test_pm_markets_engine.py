"""Anti-tautological mathematical and architectural invariant tests for `pm_markets_engine`."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from pm_markets_engine import (
    AICouncilCLI,
    AutonomousMarketsExecutor,
    AutonomousMarketsGuardian,
    CentralWalletGuard,
    JointStateSpec,
    LadderLevel,
    LifecycleScalingSpec,
    MarketScannerEvaluator,
    MarketStrategyOptimizer,
    MarketVault,
    OrderbookSnapshot,
    OutcomeSpec,
    TargetLegPlan,
    build_cli_command,
    decode_cli_response,
    integrate_ask_ladder,
    integrate_bid_ladder,
    validate_helper_reviews,
    validate_zero_anchoring_prompt,
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
        """Verifies Section 6.3: Market A and Market B have separate SQLite DBs and cannot spend each other's Maker locks."""
        vault_a = MarketVault("market_alpha", data_dir=self.data_dir)
        vault_b = MarketVault("market_beta", data_dir=self.data_dir)
        self.assertNotEqual(vault_a.db_path, vault_b.db_path)

        wallet_usdc = 1500.0
        rem_after_a = self.guard.approve_deposit(vault_a, 1000.0, on_chain_wallet_usdc=wallet_usdc)
        self.assertAlmostEqual(rem_after_a, 500.0, places=4)

        with self.assertRaises(ValueError):
            self.guard.approve_deposit(vault_b, 600.0, on_chain_wallet_usdc=wallet_usdc)

        rem_after_b = self.guard.approve_deposit(vault_b, 500.0, on_chain_wallet_usdc=wallet_usdc)
        self.assertAlmostEqual(rem_after_b, 0.0, places=4)

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

        with self.assertRaises(PermissionError):
            self.executor.cancel_market_order_by_id(vault_b, ord_a1)

        with self.assertRaises(RuntimeError):
            self.executor.cancel_all()
        with self.assertRaises(RuntimeError):
            self.executor.cancel_market_orders()

    def test_orderbook_integrals_and_proof_first_evaluator(self) -> None:
        """Verifies Section 3: Pure orderbook-integral proof without any hardcoded constants or forced templates."""
        deep_book_1 = OrderbookSnapshot(
            token_id="t1",
            bids=(LadderLevel(0.19, 5000.0), LadderLevel(0.18, 5000.0)),
            asks=(LadderLevel(0.20, 5000.0),),
            tick_size=0.001,
            min_order_size=5.0,
        )
        deep_book_2 = OrderbookSnapshot(
            token_id="t2",
            bids=(LadderLevel(0.19, 5000.0), LadderLevel(0.18, 5000.0)),
            asks=(LadderLevel(0.20, 5000.0),),
            tick_size=0.001,
            min_order_size=5.0,
        )
        deep_book_3 = OrderbookSnapshot(
            token_id="t3",
            bids=(LadderLevel(0.27, 5000.0), LadderLevel(0.26, 5000.0)),
            asks=(LadderLevel(0.28, 5000.0),),
            tick_size=0.001,
            min_order_size=5.0,
        )

        # Case 1: High-edge position vector on underpriced outcomes -> proven GO
        outcomes_go = [
            OutcomeSpec("t1", "Out_1", true_prob=0.30, target_entry_price=0.20, orderbook=deep_book_1, max_step_adverse_drop=0.02),
            OutcomeSpec("t2", "Out_2", true_prob=0.30, target_entry_price=0.20, orderbook=deep_book_2, max_step_adverse_drop=0.02),
            OutcomeSpec("t3", "Out_3", true_prob=0.32, target_entry_price=0.28, orderbook=deep_book_3, max_step_adverse_drop=0.02),
        ]
        # Asymmetric vector: 1695 shares on Out_1 & Out_2, 1150 shares on Out_3 (Total cost = $1,000.00)
        proof_go = MarketStrategyOptimizer.prove_position_vector(
            outcomes=outcomes_go,
            leg_shares={"t1": 1695.0, "t2": 1695.0, "t3": 1150.0},
            min_acceptable_rr=1.5,
        )
        self.assertTrue(proof_go.is_profitable_go)
        self.assertAlmostEqual(proof_go.total_cost_usd, 1000.0, places=4)
        self.assertAlmostEqual(proof_go.max_profit_usd, 695.0, places=4)
        self.assertAlmostEqual(proof_go.min_covered_profit_usd, 150.0, places=4)
        self.assertGreater(proof_go.effective_rr, 2.0)

        # Case 2: Overpriced / compressed market -> proven NO-GO
        outcomes_nogo = [
            OutcomeSpec("t1", "Comp_1", true_prob=0.30, target_entry_price=0.35, orderbook=deep_book_1, max_step_adverse_drop=0.03),
            OutcomeSpec("t2", "Comp_2", true_prob=0.30, target_entry_price=0.35, orderbook=deep_book_2, max_step_adverse_drop=0.03),
            OutcomeSpec("t3", "Comp_3", true_prob=0.28, target_entry_price=0.28, orderbook=deep_book_3, max_step_adverse_drop=0.03),
        ]
        proof_nogo = MarketStrategyOptimizer.prove_position_vector(
            outcomes=outcomes_nogo,
            leg_shares={"t1": 1020.0, "t2": 1020.0, "t3": 1020.0},
            min_acceptable_rr=1.0,
        )
        self.assertFalse(proof_nogo.is_profitable_go)

        # Verify non-mutually-exclusive joint state space evaluation (zero NegRisk-only bias)
        joint_states = [
            JointStateSpec("both_win", probability=0.25, winning_token_ids=("t1", "t2")),
            JointStateSpec("only_t1", probability=0.35, winning_token_ids=("t1",)),
            JointStateSpec("only_t2", probability=0.25, winning_token_ids=("t2",)),
            JointStateSpec("neither", probability=0.15, winning_token_ids=()),
        ]
        joint_proof = MarketStrategyOptimizer.prove_position_vector(
            outcomes=outcomes_go[:2],
            leg_shares={"t1": 1000.0, "t2": 1000.0},
            joint_states=joint_states,
        )
        self.assertTrue(joint_proof.is_profitable_go)
        # When both t1 and t2 win simultaneously, gross payout is 2000.0 - 400.0 cost = +1600.0 max profit
        self.assertAlmostEqual(joint_proof.total_cost_usd, 400.0, places=4)
        self.assertAlmostEqual(joint_proof.max_profit_usd, 1600.0, places=4)

        # Verify pre-maturity convergence exit (target_exit_price < 1.0) and live Ask-ladder Taker integral
        conv_book = OrderbookSnapshot(
            token_id="t_conv",
            bids=(LadderLevel(0.19, 2000.0),),
            asks=(LadderLevel(0.20, 500.0), LadderLevel(0.22, 500.0)),
            tick_size=0.001,
            min_order_size=5.0,
        )
        conv_spec = OutcomeSpec(
            token_id="t_conv",
            name="PreMaturity_Convergence",
            true_prob=0.80,
            target_entry_price=0.21,
            orderbook=conv_book,
            max_step_adverse_drop=0.01,
            target_exit_price=0.35,
            use_live_ask_integral=True,
        )
        conv_proof = MarketStrategyOptimizer.prove_position_vector(
            outcomes=[conv_spec],
            leg_shares={"t_conv": 1000.0},
        )
        # Entry cost via live Ask integral: 500*0.20 + 500*0.22 = 210.0; exit at 0.35 * 1000 = 350.0 -> +140.0 profit
        self.assertAlmostEqual(conv_proof.total_cost_usd, 210.0, places=4)
        self.assertAlmostEqual(conv_proof.max_profit_usd, 140.0, places=4)
        self.assertTrue(conv_proof.is_profitable_go)

        # Verify dual-side (YES + NO) untruncated token extraction
        tokens = MarketScannerEvaluator.extract_event_tokens(
            {
                "markets": [
                    {
                        "id": "m1",
                        "question": "Contender A?",
                        "active": True,
                        "closed": False,
                        "clobTokenIds": '["tok_yes_1", "tok_no_1"]',
                        "outcomes": '["Yes", "No"]',
                        "orderPriceMinTickSize": 0.001,
                        "orderMinSize": 5,
                    }
                ]
            }
        )
        self.assertEqual(len(tokens), 2)
        self.assertEqual({t["outcome_side"] for t in tokens}, {"Yes", "No"})

        # Verify exact Ask/Bid ladder integration math
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

        # Verify deterministic select_optimal_basket picks the mathematical R/R peak without human discretion or magic constants
        opt_report = MarketStrategyOptimizer.select_optimal_basket(
            outcomes=outcomes_go,
            budget_usd=400.0,
        )
        self.assertTrue(opt_report.is_profitable_go)
        self.assertGreater(opt_report.effective_rr, 1.0)
        self.assertGreater(len(opt_report.leg_shares), 0)

    def test_three_way_reconciliation_and_guardian(self) -> None:
        """Verifies Section 6.1 & 6.2: 3-Way Truth Reconciliation and Auto-Pegging."""
        vault = MarketVault("test_reconcile", data_dir=self.data_dir)
        self.guard.approve_deposit(vault, 500.0, on_chain_wallet_usdc=1000.0)
        vault.upsert_target_leg(
            TargetLegPlan(
                token_id="tok_x",
                outcome_name="Contender_X",
                role_set="core",
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
        vault.record_order_fill(oid, newly_matched_shares=600.0, fill_price=0.22)
        rows_partial = self.executor.reconcile_market_state(vault)
        self.assertEqual(len(rows_partial), 1)
        self.assertFalse(rows_partial[0].is_verified_on_chain)
        self.assertAlmostEqual(rows_partial[0].wallet_shares, 600.0, places=4)
        self.assertAlmostEqual(rows_partial[0].wallet_vwap, 0.22, places=4)
        self.assertAlmostEqual(rows_partial[0].wallet_shortfall, 400.0, places=4)
        self.assertAlmostEqual(rows_partial[0].unplaced_discrepancy, 0.0, places=4)
        self.assertAlmostEqual(rows_partial[0].open_order_shares, 400.0, places=4)

        vault.record_order_fill(oid, newly_matched_shares=400.0, fill_price=0.24)
        rows_done = self.executor.reconcile_market_state(vault)
        self.assertTrue(rows_done[0].is_verified_on_chain)
        # Exact VWAP across 600 sh @ 0.22 and 400 sh @ 0.24 is (132 + 96) / 1000 = 0.228
        self.assertAlmostEqual(rows_done[0].wallet_vwap, 0.228, places=4)
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

    def test_gpt_6_astra_and_claude_opus_5_5_cli_contract(self) -> None:
        """Verifies Section 7.3 & 7.4: exact CLI contract for gpt-6-astra and claude-opus-5-5, zero-tool & zero-anchoring."""
        council = AICouncilCLI(lock_dir=self.data_dir / "ai_locks")
        codex_cfg = council.get_provider_config("codex")
        claude_cfg = council.get_provider_config("claude")

        self.assertEqual(codex_cfg["model"], "gpt-6-astra")
        self.assertEqual(claude_cfg["model"], "claude-opus-5-5")

        clean_prompt = "Deduce the optimal share allocation vector from first principles given the observed orderbook integral."
        validate_zero_anchoring_prompt(clean_prompt)

        with self.assertRaises(ValueError):
            validate_zero_anchoring_prompt("Model the distribution (e.g. Poisson or Copula) for this market.")

        argv_codex, _, stdin_codex = build_cli_command("codex", codex_cfg, self.data_dir, clean_prompt)
        self.assertEqual(
            argv_codex[:9],
            [
                "/Users/belavarga/.local/bin/codex",
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "--model",
            ],
        )
        self.assertEqual(argv_codex[9], "gpt-6-astra")
        self.assertIsNone(stdin_codex)

        argv_claude, _, stdin_claude = build_cli_command("claude", claude_cfg, self.data_dir, clean_prompt)
        self.assertEqual(
            argv_claude,
            [
                "/Users/belavarga/.local/bin/claude",
                "--print",
                "--safe-mode",
                "--restricted",
                "--tools",
                "",
                "--strict-mcp-config",
                "--no-session-persistence",
                "--output-format",
                "json",
                "--model",
                "claude-opus-5-5",
            ],
        )
        self.assertEqual(stdin_claude, clean_prompt)

        claude_ok_stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "permission_denials": [],
                "subagent_stats": {"spawned": 0},
                "result": "First-principles allocation verified.",
                "modelUsage": {"claude-opus-5-5": {"canonicalModel": "claude-opus-5-5"}},
            }
        )
        st_ok, ans_ok = decode_cli_response(
            "claude",
            {"returncode": 0, "state": None, "stdout": claude_ok_stdout, "stderr": ""},
            expected_model="claude-opus-5-5",
        )
        self.assertEqual(st_ok, "received_unreviewed")
        self.assertEqual(ans_ok, "First-principles allocation verified.")

        claude_bad_tool = json.dumps(
            {
                "type": "result",
                "is_error": False,
                "permission_denials": ["Bash"],
                "result": "Attempted tool use",
                "modelUsage": {"claude-opus-5-5": {"canonicalModel": "claude-opus-5-5"}},
            }
        )
        st_bad, _ = decode_cli_response(
            "claude",
            {"returncode": 0, "state": None, "stdout": claude_bad_tool, "stderr": ""},
            expected_model="claude-opus-5-5",
        )
        self.assertEqual(st_bad, "unexpected_tool_activity")

        advice = [{"id": "adv_1", "sha256": "abc123"}]
        valid_review = [{"id": "adv_1", "sha256": "abc123", "disposition": "used", "reason": "Verified integral proof"}]
        self.assertEqual(len(validate_helper_reviews(valid_review, advice)), 1)

    def test_unified_market_strategy_and_conditional_x_05x_scaling(self) -> None:
        """Verifies unified YES vs NO strategy discovery and $X + $0.5X/week conditional scaling invariants."""
        # Scenario A: Overpriced YES favorites with unlisted contenders -> Optimizer must discover NO Synthetic Field Basket
        no_specs = [
            OutcomeSpec(
                token_id="georgia_no",
                name="Georgia NO",
                true_prob=1.0 - 0.145,
                target_entry_price=0.800,
                orderbook=OrderbookSnapshot(
                    token_id="georgia_no",
                    bids=(LadderLevel(0.790, 20000.0),),
                    asks=(LadderLevel(0.800, 20000.0),),
                    tick_size=0.001,
                    min_order_size=5.0,
                ),
                use_live_ask_integral=True,
            ),
            OutcomeSpec(
                token_id="nd_no",
                name="Notre Dame NO",
                true_prob=1.0 - 0.125,
                target_entry_price=0.830,
                orderbook=OrderbookSnapshot(
                    token_id="nd_no",
                    bids=(LadderLevel(0.820, 20000.0),),
                    asks=(LadderLevel(0.830, 20000.0),),
                    tick_size=0.001,
                    min_order_size=5.0,
                ),
                use_live_ask_integral=True,
            ),
            OutcomeSpec(
                token_id="bama_no",
                name="Alabama NO",
                true_prob=1.0 - 0.045,
                target_entry_price=0.880,
                orderbook=OrderbookSnapshot(
                    token_id="bama_no",
                    bids=(LadderLevel(0.870, 20000.0),),
                    asks=(LadderLevel(0.880, 20000.0),),
                    tick_size=0.001,
                    min_order_size=5.0,
                ),
                use_live_ask_integral=True,
            ),
            OutcomeSpec(
                token_id="miami_no",
                name="Miami NO",
                true_prob=1.0 - 0.135,
                target_entry_price=0.830,
                orderbook=OrderbookSnapshot(
                    token_id="miami_no",
                    bids=(LadderLevel(0.820, 20000.0),),
                    asks=(LadderLevel(0.830, 20000.0),),
                    tick_size=0.001,
                    min_order_size=5.0,
                ),
                use_live_ask_integral=True,
            ),
            OutcomeSpec(
                token_id="texas_no",
                name="Texas NO",
                true_prob=1.0 - 0.055,
                target_entry_price=0.900,
                orderbook=OrderbookSnapshot(
                    token_id="texas_no",
                    bids=(LadderLevel(0.890, 20000.0),),
                    asks=(LadderLevel(0.900, 20000.0),),
                    tick_size=0.001,
                    min_order_size=5.0,
                ),
                use_live_ask_integral=True,
            ),
            OutcomeSpec(
                token_id="indiana_no",
                name="Indiana NO",
                true_prob=1.0 - 0.115,
                target_entry_price=0.900,
                orderbook=OrderbookSnapshot(
                    token_id="indiana_no",
                    bids=(LadderLevel(0.890, 20000.0),),
                    asks=(LadderLevel(0.900, 20000.0),),
                    tick_size=0.001,
                    min_order_size=5.0,
                ),
                use_live_ask_integral=True,
            ),
        ]
        scaling = LifecycleScalingSpec(
            initial_capital_x_usd=1000.0,
            weekly_addition_fraction=0.50,
            active_opportunity_weeks=6,
        )
        unified_no = MarketStrategyOptimizer.evaluate_unified_market_strategy(
            market_name="NCAA_2026_Test",
            yes_outcomes=None,
            no_outcomes=no_specs,
            scaling_spec=scaling,
            min_acceptable_rr=1.0,
        )
        self.assertTrue(unified_no.is_verified_go)
        self.assertEqual(unified_no.strategy_family, "NEGRISK_NO_SYNTHETIC_FIELD")
        self.assertAlmostEqual(unified_no.initial_capital_x_usd, 1000.0, places=2)
        self.assertAlmostEqual(unified_no.weekly_tranche_05x_usd, 500.0, places=2)
        self.assertAlmostEqual(unified_no.total_scaled_capital_usd, 4000.0, places=2)
        # On $1,000 initial capital, 6-leg NO basket worst-case loss is only ~$27.05 (2.7%), never $1,000
        self.assertLess(unified_no.initial_x_proof.max_loss_guarded_usd, 35.0)
        self.assertGreater(unified_no.lifecycle_effective_rr, 1.0)
        self.assertIn("CHECK_3_POSITIVE_INTRINSIC_EDGE_VERIFIED", unified_no.verification_checks)


if __name__ == "__main__":
    unittest.main()
