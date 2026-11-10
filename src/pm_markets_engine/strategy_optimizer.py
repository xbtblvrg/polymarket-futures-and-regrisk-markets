"""Unconstrained First-Principles Strategy & Proof Evaluator (`MarketStrategyOptimizer`).

Implements Sections 1, 3, and 4 of the Master Blueprint (`Zero-Anchoring, Zero-Memory & Proof-First`):
- Zero hardcoded constants, zero forced basket templates, zero arbitrary clamps.
- Evaluates any first-principles position vector `q` strictly from live Ask/Bid orderbook integrals
  and verified probability distributions, proving:
    1. MaxLoss_guarded (purely from Bid-ladder integral and step shock, or full cost if unabsorbable),
    2. MaxProfit,
    3. Realistic Expected Profit (E[PnL_real]),
    4. Effective Risk/Reward Ratio (R/R).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .scanner_evaluator import OrderbookSnapshot, integrate_ask_ladder, integrate_bid_ladder


@dataclass(frozen=True)
class OutcomeSpec:
    token_id: str
    name: str
    true_prob: float
    target_entry_price: float
    orderbook: OrderbookSnapshot
    max_step_adverse_drop: float = 0.0  # Empirical max single-window price drop (\Delta p_i)


@dataclass(frozen=True)
class PositionProofReport:
    total_cost_usd: float
    max_profit_usd: float
    min_covered_profit_usd: float
    realistic_expected_profit_usd: float
    endogenous_bid_liquidation_usd: float
    max_loss_guarded_usd: float
    naive_maturity_max_loss_usd: float
    effective_rr: float
    leg_shares: Dict[str, float]
    is_profitable_go: bool
    proof_summary: str


class MarketStrategyOptimizer:
    """Pure first-principles mathematical evaluator with zero template anchoring or magic constants."""

    @staticmethod
    def prove_position_vector(
        outcomes: List[OutcomeSpec],
        leg_shares: Dict[str, float],
        has_discontinuous_gap_risk: bool = False,
        min_acceptable_rr: float = 1.0,
    ) -> PositionProofReport:
        """Proves the 4 mandatory metrics (MaxLoss_guarded, MaxProfit, Realistic E[PnL], R/R) for any position vector `q`."""
        active_outcomes = [o for o in outcomes if leg_shares.get(o.token_id, 0.0) > 0.0]
        if not active_outcomes:
            raise ValueError("Position vector must contain at least one positive leg.")

        total_cost = sum(leg_shares[o.token_id] * o.target_entry_price for o in active_outcomes)
        total_cost = round(total_cost, 6)

        net_profits_if_win = {
            o.token_id: round(leg_shares[o.token_id] - total_cost, 6)
            for o in active_outcomes
        }
        max_profit = max(net_profits_if_win.values())
        min_covered_profit = min(net_profits_if_win.values())

        # Endogenous Bid-Ladder Liquidation Integral: \sum_i \int_0^{q_i} Bid_i(u) du
        total_bid_liq = 0.0
        step_shock_usd = 0.0
        all_absorbable = True

        for o in active_outcomes:
            sh = leg_shares[o.token_id]
            bid_int = integrate_bid_ladder(o.orderbook.bids, sh)
            total_bid_liq += bid_int.total_usdc
            step_shock_usd += sh * max(0.0, o.max_step_adverse_drop)
            if not bid_int.fully_absorbable:
                all_absorbable = False

        total_bid_liq = round(total_bid_liq, 6)
        if has_discontinuous_gap_risk or not all_absorbable or total_bid_liq <= 0.0:
            max_loss_guarded = total_cost
        else:
            immediate_liquidation_gap = max(0.0, total_cost - total_bid_liq)
            max_loss_guarded = round(min(total_cost, immediate_liquidation_gap + step_shock_usd), 6)
            if max_loss_guarded <= 0.0:
                # Even when Bid > entry cost, bound denominator by minimum 1-tick liquidation friction
                min_tick_friction = sum(leg_shares[o.token_id] * o.orderbook.tick_size for o in active_outcomes)
                max_loss_guarded = round(max(1e-6, min_tick_friction), 6)

        covered_prob_sum = sum(o.true_prob for o in active_outcomes)
        uncovered_prob = max(0.0, 1.0 - covered_prob_sum)

        realistic_expected_profit = round(
            sum(o.true_prob * net_profits_if_win[o.token_id] for o in active_outcomes)
            - uncovered_prob * max_loss_guarded,
            6,
        )

        effective_rr = round(realistic_expected_profit / max_loss_guarded, 4) if max_loss_guarded > 0 else 0.0
        is_go = (realistic_expected_profit > 0.0 and max_profit > 0.0 and effective_rr >= min_acceptable_rr)

        summary = (
            f"{'GO' if is_go else 'NO_GO'} | Cost=${total_cost:.2f} | "
            f"MaxProfit=+${max_profit:.2f} | RealisticE[PnL]=${realistic_expected_profit:+.2f} | "
            f"GuardedMaxLoss=-${max_loss_guarded:.2f} (Naive=-${total_cost:.2f}) | R/R={effective_rr:.2f}x"
        )

        return PositionProofReport(
            total_cost_usd=total_cost,
            max_profit_usd=max_profit,
            min_covered_profit_usd=min_covered_profit,
            realistic_expected_profit_usd=realistic_expected_profit,
            endogenous_bid_liquidation_usd=total_bid_liq,
            max_loss_guarded_usd=max_loss_guarded,
            naive_maturity_max_loss_usd=total_cost,
            effective_rr=effective_rr,
            leg_shares={k: round(v, 6) for k, v in leg_shares.items() if v > 0.0},
            is_profitable_go=is_go,
            proof_summary=summary,
        )

    @staticmethod
    def verify_monotonic_value_guard(
        value_before: float,
        value_after_net_of_spread: float,
    ) -> bool:
        """Section 4.1: Rebalance / Swing is permitted ONLY if net value strictly improves after spread."""
        return value_after_net_of_spread > value_before + 1e-6

    @staticmethod
    def evaluate_unfilled_leg_reoptimization(
        unfilled_shares: float,
        planned_limit_price: float,
        live_book: OrderbookSnapshot,
        true_prob: float,
        current_expected_profit: float,
    ) -> Tuple[str, Dict[str, float]]:
        """Section 6.2.2: Chooses between Taker completion and capital redistribution purely by net E[PnL] proof."""
        ask_int = integrate_ask_ladder(live_book.asks, unfilled_shares)
        if not ask_int.fully_absorbable:
            return ("REDISTRIBUTE_OR_WAIT", {"released_usdc": round(unfilled_shares * planned_limit_price, 6)})

        extra_cost = max(0.0, ask_int.total_usdc - (unfilled_shares * planned_limit_price))
        if ask_int.vwap_price < true_prob and (current_expected_profit - extra_cost) > 0.0:
            return (
                "CONTROLLED_TAKER_COMPLETION",
                {
                    "taker_vwap": ask_int.vwap_price,
                    "extra_cost_usd": round(extra_cost, 6),
                    "shares_to_buy": unfilled_shares,
                },
            )
        return (
            "REDISTRIBUTE_OR_WAIT",
            {
                "released_usdc": round(unfilled_shares * planned_limit_price, 6),
                "current_ask_vwap": ask_int.vwap_price,
            },
        )
