"""Reality-First Strategy Optimizer & Risk/Reward Engine (`MarketStrategyOptimizer`).

Implements Sections 1.1.2, 3.1-3.3, 3.8.1, 5.1, and 7.2.3 of the Master Blueprint:
- Evaluates any market starting from reality & feasibility (never forcing pre-existing templates).
- Computes Graded Asymmetric Payout Allocation (`S_alpha` vs `S_base`).
- Computes Endogenous Orderbook-Integral Liquidation Value & `MaxLoss_guarded`.
- Enforces `Monotonic Payout Guard` (Section 5.1).
- Performs Dynamic Strategy Re-Optimization when Maker legs remain unfilled (Section 7.2.3).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .scanner_evaluator import OrderbookSnapshot, integrate_ask_ladder, integrate_bid_ladder


@dataclass(frozen=True)
class OutcomeCandidate:
    token_id: str
    name: str
    true_prob: float
    entry_price: float
    role_set: str  # 'S_alpha', 'S_base', 'S_defer', 'S_noise'
    orderbook: OrderbookSnapshot


@dataclass(frozen=True)
class GradedAllocationPlan:
    budget_usd: float
    q_alpha: float
    q_base: float
    alpha_net_profit: float
    base_net_profit: float
    expected_passive_profit: float
    endogenous_liquidation_value: float
    max_loss_guarded: float
    effective_rr: float
    leg_shares: Dict[str, float]
    is_viable_go: bool
    verdict_reason: str


class MarketStrategyOptimizer:
    """First-principles optimizer for multi-option / futures / barrier Polymarket structures."""

    @staticmethod
    def build_graded_asymmetric_plan(
        candidates: List[OutcomeCandidate],
        budget_usd: float,
        base_return_floor: float,
        has_absorbing_gap_risk: bool = False,
        min_required_rr: float = 1.0,
    ) -> GradedAllocationPlan:
        """Computes two-tier asymmetric allocation (`S_alpha` and `S_base`) and evaluates Reality-First GO/NO-GO."""
        if budget_usd <= 0.0:
            raise ValueError("budget_usd must be positive.")

        alpha_legs = [c for c in candidates if c.role_set == "S_alpha"]
        base_legs = [c for c in candidates if c.role_set == "S_base"]

        sum_p_base = sum(c.entry_price for c in base_legs)
        sum_p_alpha = sum(c.entry_price for c in alpha_legs)

        q_base = round(budget_usd * (1.0 + base_return_floor), 6) if base_legs else 0.0
        cost_base = q_base * sum_p_base

        if cost_base >= budget_usd or sum_p_alpha <= 0.0:
            return GradedAllocationPlan(
                budget_usd=budget_usd,
                q_alpha=0.0,
                q_base=q_base,
                alpha_net_profit=-budget_usd,
                base_net_profit=q_base - budget_usd,
                expected_passive_profit=-budget_usd,
                endogenous_liquidation_value=0.0,
                max_loss_guarded=budget_usd,
                effective_rr=0.0,
                leg_shares={},
                is_viable_go=False,
                verdict_reason="NO_GO_COMPRESSION: Base shield cost exhausts budget or no viable alpha legs exist.",
            )

        rem_budget = budget_usd - cost_base
        q_alpha = round(rem_budget / sum_p_alpha, 6)

        leg_shares: Dict[str, float] = {}
        for c in alpha_legs:
            leg_shares[c.token_id] = q_alpha
        for c in base_legs:
            leg_shares[c.token_id] = q_base

        alpha_net_profit = round(q_alpha - budget_usd, 6)
        base_net_profit = round(q_base - budget_usd, 6) if base_legs else 0.0

        p_alpha_total = sum(c.true_prob for c in alpha_legs)
        p_base_total = sum(c.true_prob for c in base_legs)
        p_uncovered = max(0.0, 1.0 - (p_alpha_total + p_base_total))

        # Endogenous Orderbook Liquidation Integral (Section 3.8.1)
        total_liq_value = 0.0
        all_absorbable = True
        for c in alpha_legs + base_legs:
            sh = leg_shares[c.token_id]
            bid_int = integrate_bid_ladder(c.orderbook.bids, sh)
            total_liq_value += bid_int.total_usdc
            if not bid_int.fully_absorbable:
                all_absorbable = False

        total_liq_value = round(total_liq_value, 6)
        if has_absorbing_gap_risk or not all_absorbable or total_liq_value <= 0.0:
            max_loss_guarded = round(budget_usd, 6)
        else:
            # Immediate liquidation loss + potential single-step adverse move buffer
            immediate_spread_loss = max(0.0, budget_usd - total_liq_value)
            max_loss_guarded = round(min(budget_usd, max(immediate_spread_loss, 0.15 * budget_usd)), 6)

        expected_passive_profit = round(
            p_alpha_total * alpha_net_profit
            + p_base_total * base_net_profit
            - p_uncovered * max_loss_guarded,
            6,
        )

        effective_rr = round(expected_passive_profit / max_loss_guarded, 4) if max_loss_guarded > 0 else 0.0
        is_go = (
            alpha_net_profit > base_net_profit >= 0.0
            and expected_passive_profit > 0.0
            and effective_rr >= min_required_rr
        )
        reason = (
            f"GO: E[PnL]=+${expected_passive_profit:.2f}, MaxProfit=+${alpha_net_profit:.2f}, "
            f"GuardedMaxLoss=-${max_loss_guarded:.2f}, R/R={effective_rr:.2f}x"
            if is_go
            else f"NO_GO: Suboptimal R/R ({effective_rr:.2f}x < {min_required_rr:.2f}x) or negative E[PnL] (${expected_passive_profit:.2f})"
        )

        return GradedAllocationPlan(
            budget_usd=round(budget_usd, 6),
            q_alpha=q_alpha,
            q_base=q_base,
            alpha_net_profit=alpha_net_profit,
            base_net_profit=base_net_profit,
            expected_passive_profit=expected_passive_profit,
            endogenous_liquidation_value=total_liq_value,
            max_loss_guarded=max_loss_guarded,
            effective_rr=effective_rr,
            leg_shares=leg_shares,
            is_viable_go=is_go,
            verdict_reason=reason,
        )

    @staticmethod
    def verify_monotonic_payout_guard(
        payout_floor_before: float,
        payout_floor_after_net_of_spread: float,
    ) -> bool:
        """Section 5.1: Rebalance / Swing is allowed ONLY if net payout floor strictly increases."""
        return payout_floor_after_net_of_spread > payout_floor_before + 1e-6

    @staticmethod
    def evaluate_unfilled_leg_action(
        unfilled_shares: float,
        planned_limit_price: float,
        live_book: OrderbookSnapshot,
        true_prob: float,
        current_payout_floor: float,
        max_floor_dilution_tol: float = 0.02,
    ) -> Tuple[str, Dict[str, float]]:
        """Section 7.2.3: Chooses between Controlled Taker Completion (A) and Role Reclassification (B)."""
        ask_int = integrate_ask_ladder(live_book.asks, unfilled_shares)
        if not ask_int.fully_absorbable:
            return ("ROLE_RECLASSIFICATION_REDISTRIBUTE", {"released_usdc": round(unfilled_shares * planned_limit_price, 6)})

        extra_cost = max(0.0, ask_int.total_usdc - (unfilled_shares * planned_limit_price))
        rel_dilution = extra_cost / max(1.0, current_payout_floor)

        if ask_int.vwap_price <= true_prob and rel_dilution <= max_floor_dilution_tol:
            return (
                "CONTROLLED_TAKER_COMPLETION",
                {
                    "taker_vwap": ask_int.vwap_price,
                    "extra_cost_usd": round(extra_cost, 6),
                    "shares_to_buy": unfilled_shares,
                },
            )
        return (
            "ROLE_RECLASSIFICATION_REDISTRIBUTE",
            {
                "released_usdc": round(unfilled_shares * planned_limit_price, 6),
                "overheated_ask_vwap": ask_int.vwap_price,
            },
        )
