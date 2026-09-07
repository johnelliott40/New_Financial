"""Tests for modules/investing.py — tax-lot ledger construction + per-asset roll-forward +
Stage-1 sales (MODEL_WIRING.md §3-§5). The contribution waterfall that used to live here
(`contribution_waterfall`/`allocate_401k_family_remaining`) moved to modules/contributions.py's
mode-based system 2026-08-16 (CONTRIBUTION_TOGGLE_REDESIGN.md) — see tests/test_contributions.py."""

from __future__ import annotations

import pytest

from modules.investing import (
    DEFAULT_DRAW_ORDER,
    REBALANCEABLE_ACCOUNT_TYPES,
    WITHDRAWAL_STRATEGIES,
    annual_withdrawal_target,
    bracket_aware_draw,
    create_lots,
    draw_order_fill,
    rebalance_account,
    roll_forward_holding,
    roll_forward_portfolio,
    sell_lots,
)
from modules.tax import load_bracket_table


@pytest.fixture
def bracket_table():
    return load_bracket_table()


def _universe(**tickers):
    """tickers: {ticker: {"asset_class", "nominal_return", "expense_ratio", "dividend_rate", "income_type"}}"""
    asset_classes = {info["asset_class"]: {"label": info["asset_class"], "nominal_return": info["nominal_return"]} for info in tickers.values()}
    return {
        "asset_classes": asset_classes,
        "etfs": {
            t: {
                "asset_class": info["asset_class"],
                "expense_ratio": info["expense_ratio"],
                "dividend_rate": info["dividend_rate"],
                "income_type": info["income_type"],
            }
            for t, info in tickers.items()
        },
    }


# ---- create_lots ----


class TestCreateLots:
    def test_splits_dollars_by_ticker_weight_within_an_account_type(self):
        lots = create_lots(
            dollars_by_account_type={"Taxable": 1000.0},
            target_allocations={"Taxable": {"VTI": 0.6, "BND": 0.4}},
            prices_by_ticker={"VTI": 250.0, "BND": 80.0},
            year=2026,
        )
        vti_lot = next(l for l in lots if l["ticker"] == "VTI")
        bnd_lot = next(l for l in lots if l["ticker"] == "BND")
        assert vti_lot["shares"] == pytest.approx(600.0 / 250.0)
        assert bnd_lot["shares"] == pytest.approx(400.0 / 80.0)
        assert vti_lot["basis_per_share"] == pytest.approx(250.0)
        assert vti_lot["account_type"] == "Taxable"
        assert vti_lot["year_acquired"] == 2026

    def test_normalizes_weights_that_dont_sum_to_1(self):
        # 0.3 + 0.3 = 0.6, not 1.0 -- still splits proportionally (60/40 between the two), not
        # silently investing only 60% of the dollars.
        lots = create_lots(
            dollars_by_account_type={"Taxable": 1000.0},
            target_allocations={"Taxable": {"VTI": 0.3, "BND": 0.3}},
            prices_by_ticker={"VTI": 100.0, "BND": 100.0},
            year=2026,
        )
        total_shares_value = sum(l["shares"] * l["basis_per_share"] for l in lots)
        assert total_shares_value == pytest.approx(1000.0)

    def test_zero_dollars_produces_no_lots(self):
        lots = create_lots(
            dollars_by_account_type={"Taxable": 0.0},
            target_allocations={"Taxable": {"VTI": 1.0}},
            prices_by_ticker={"VTI": 100.0},
            year=2026,
        )
        assert lots == []

    def test_account_type_with_no_target_allocation_produces_no_lots_but_does_not_raise(self):
        lots = create_lots(
            dollars_by_account_type={"Roth IRA": 5000.0},
            target_allocations={},  # nothing configured for Roth IRA
            prices_by_ticker={"VTI": 100.0},
            year=2026,
        )
        assert lots == []

    def test_ticker_with_no_known_price_is_skipped_not_a_crash(self):
        lots = create_lots(
            dollars_by_account_type={"Taxable": 1000.0},
            target_allocations={"Taxable": {"VTI": 0.5, "GHOST": 0.5}},
            prices_by_ticker={"VTI": 100.0},  # GHOST has no price
            year=2026,
        )
        assert len(lots) == 1
        assert lots[0]["ticker"] == "VTI"

    def test_multiple_account_types_produce_independent_lots(self):
        lots = create_lots(
            dollars_by_account_type={"Taxable": 1000.0, "Roth IRA": 500.0},
            target_allocations={"Taxable": {"VTI": 1.0}, "Roth IRA": {"VTI": 1.0}},
            prices_by_ticker={"VTI": 100.0},
            year=2026,
        )
        assert len(lots) == 2
        account_types = {l["account_type"] for l in lots}
        assert account_types == {"Taxable", "Roth IRA"}


# ---- roll_forward_holding (MODEL_WIRING.md §4.1) ----


class TestRollForwardHolding:
    def test_no_dividend_price_return_equals_full_total_return(self):
        # dividend_rate=0 -> nothing subtracted -> real_price_return == the full real total return.
        result = roll_forward_holding(
            price=100.0, nominal_return=0.08, expense_ratio=0.0, inflation_rate=0.025, dividend_rate=0.0
        )
        expected_real_return = (1.08 / 1.025) - 1.0
        assert result["new_price"] == pytest.approx(100.0 * (1.0 + expected_real_return))
        assert result["distribution_income_per_share"] == pytest.approx(0.0)

    def test_dividend_rate_is_geometrically_divided_out_of_price_return_not_subtracted(self):
        # THE double-counting trap (§4.1's own words), fixed via the SAME geometric decomposition
        # `expense_adjusted_return` uses: price return must be LOWER than the no-dividend case, but
        # by the geometric division `(1 + total) / (1 + div) - 1`, not the plain subtraction
        # `total - div` (those two differ by a small cross term that used to leak extra growth).
        no_div = roll_forward_holding(
            price=100.0, nominal_return=0.08, expense_ratio=0.0, inflation_rate=0.025, dividend_rate=0.0
        )
        with_div = roll_forward_holding(
            price=100.0, nominal_return=0.08, expense_ratio=0.0, inflation_rate=0.025, dividend_rate=0.02
        )
        no_div_return = no_div["new_price"] / 100.0 - 1.0
        with_div_return = with_div["new_price"] / 100.0 - 1.0
        assert (1.0 + with_div_return) == pytest.approx((1.0 + no_div_return) / 1.02)
        assert with_div_return < no_div_return  # still strictly lower -- still not double-counted
        assert with_div["distribution_income_per_share"] == pytest.approx(2.0)  # 100 * 0.02

    def test_money_market_loses_real_value_every_year(self):
        # Entire nominal return IS the interest/dividend rate -> ~0% nominal price return -> NEGATIVE
        # real price return once inflation is subtracted. Correct, not a bug (§4.1's own example).
        result = roll_forward_holding(
            price=1.0, nominal_return=0.04, expense_ratio=0.001, inflation_rate=0.025, dividend_rate=0.04
        )
        assert result["new_price"] < 1.0
        # Explicit closed-form check of the new geometric formula (not just "< 1.0"), since dividend
        #_rate == the full nominal return here is the extreme case most likely to expose a formula
        # slip: expense-adjust, then real-adjust, then geometrically divide out the dividend rate.
        net_of_expense = (1.04 / 1.001) - 1.0
        real_total_return = (1.0 + net_of_expense) / 1.025 - 1.0
        expected_real_price_return = (1.0 + real_total_return) / 1.04 - 1.0
        assert result["new_price"] == pytest.approx(1.0 * (1.0 + expected_real_price_return))

    def test_expense_ratio_reduces_price_return(self):
        cheap = roll_forward_holding(
            price=100.0, nominal_return=0.08, expense_ratio=0.0003, inflation_rate=0.025, dividend_rate=0.0
        )
        expensive = roll_forward_holding(
            price=100.0, nominal_return=0.08, expense_ratio=0.01, inflation_rate=0.025, dividend_rate=0.0
        )
        assert expensive["new_price"] < cheap["new_price"]


# ---- roll_forward_portfolio (MODEL_WIRING.md §4.1-§4.3) ----


class TestRollForwardPortfolio:
    def test_zero_dividend_single_asset_matches_closed_form_price_compounding(self):
        # The clean, unambiguous version of the required §9 closed-form test: with NO dividend at
        # all, there's no reinvestment to reason about -- price return IS total return exactly, and
        # N years of pure price compounding must equal the textbook closed form. Isolates the "no
        # double-counting" proof from the separate (and separately correct -- see the next test)
        # question of how discrete annual reinvestment-then-growth compounds.
        universe = _universe(
            VTI={"asset_class": "US", "nominal_return": 0.08, "expense_ratio": 0.0, "dividend_rate": 0.0, "income_type": "qualified"}
        )
        inflation_rate = 0.025
        real_total_return = (1.08 / 1.025) - 1.0

        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 100.0, "basis_per_share": 100.0}]
        prices = {"VTI": 100.0}
        initial_value = lots[0]["shares"] * prices["VTI"]

        for year in range(10):
            result = roll_forward_portfolio(lots, universe, prices, inflation_rate, withdrawing_fraction=0.0)
            assert result["new_lots"] == []  # no dividend -> nothing to reinvest
            prices = result["updated_prices_by_ticker"]

        final_value = sum(lot["shares"] * prices[lot["ticker"]] for lot in lots)
        expected_value = initial_value * (1.0 + real_total_return) ** 10
        assert final_value == pytest.approx(expected_value, rel=1e-9)

    def test_dividend_reinvestment_reproduces_total_return_exactly_no_cross_term(self):
        # With a nonzero dividend, full reinvestment happens at the START-of-year price and those
        # new shares then participate in that SAME year's price growth (§3.4's beginning-of-year
        # convention) -- (1 + dividend_rate) * (1 + real_price_return) must equal exactly (1 +
        # real_total_return) by construction (the geometric split divides the dividend rate back
        # out), so N years of reinvestment must equal the SAME closed form as the zero-dividend case
        # above, regardless of dividend yield -- no leftover cross term, unlike the old subtractive
        # split this replaces.
        universe = _universe(
            VTI={"asset_class": "US", "nominal_return": 0.08, "expense_ratio": 0.0, "dividend_rate": 0.02, "income_type": "qualified"}
        )
        inflation_rate = 0.025
        real_total_return = (1.08 / 1.025) - 1.0

        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 100.0, "basis_per_share": 100.0}]
        prices = {"VTI": 100.0}
        initial_value = lots[0]["shares"] * prices["VTI"]

        for year in range(10):
            result = roll_forward_portfolio(lots, universe, prices, inflation_rate, withdrawing_fraction=0.0)
            for lot in result["new_lots"]:
                lot["year_acquired"] = 2026 + year
            lots = lots + result["new_lots"]
            prices = result["updated_prices_by_ticker"]

        final_value = sum(lot["shares"] * prices[lot["ticker"]] for lot in lots)
        expected_value = initial_value * (1.0 + real_total_return) ** 10
        assert final_value == pytest.approx(expected_value, rel=1e-9)

    def test_distribution_routed_to_correct_account_and_income_type(self):
        universe = _universe(
            VTI={"asset_class": "US", "nominal_return": 0.08, "expense_ratio": 0.0, "dividend_rate": 0.02, "income_type": "qualified"},
            SGOV={"asset_class": "CASH", "nominal_return": 0.04, "expense_ratio": 0.001, "dividend_rate": 0.04, "income_type": "interest"},
        )
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 100.0, "basis_per_share": 100.0},
            {"ticker": "SGOV", "account_type": "Traditional 401(k)", "shares": 100.0, "basis_per_share": 100.0},
        ]
        prices = {"VTI": 100.0, "SGOV": 100.0}
        result = roll_forward_portfolio(lots, universe, prices, 0.025, withdrawing_fraction=0.0)

        assert result["distribution_by_account_and_type"]["Taxable"]["qualified"] == pytest.approx(200.0)
        assert result["distribution_by_account_and_type"]["Taxable"]["ordinary"] == pytest.approx(0.0)
        assert result["distribution_by_account_and_type"]["Taxable"]["interest"] == pytest.approx(0.0)
        assert result["distribution_by_account_and_type"]["Traditional 401(k)"]["interest"] == pytest.approx(400.0)

    def test_no_blended_rate_each_ticker_uses_its_own_return(self):
        # Two tickers with very different returns in the SAME portfolio must produce independently
        # different new prices -- never a portfolio-wide blended rate.
        universe = _universe(
            HIGH={"asset_class": "A", "nominal_return": 0.12, "expense_ratio": 0.0, "dividend_rate": 0.0, "income_type": "qualified"},
            LOW={"asset_class": "B", "nominal_return": 0.02, "expense_ratio": 0.0, "dividend_rate": 0.0, "income_type": "qualified"},
        )
        lots = [
            {"ticker": "HIGH", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 100.0},
            {"ticker": "LOW", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 100.0},
        ]
        prices = {"HIGH": 100.0, "LOW": 100.0}
        result = roll_forward_portfolio(lots, universe, prices, 0.025, withdrawing_fraction=0.0)
        assert result["updated_prices_by_ticker"]["HIGH"] > result["updated_prices_by_ticker"]["LOW"]

    def test_taxable_reinvestment_stops_once_withdrawing(self):
        universe = _universe(
            VTI={"asset_class": "US", "nominal_return": 0.08, "expense_ratio": 0.0, "dividend_rate": 0.02, "income_type": "qualified"}
        )
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 100.0, "basis_per_share": 100.0}]
        prices = {"VTI": 100.0}
        accumulating = roll_forward_portfolio(lots, universe, prices, 0.025, withdrawing_fraction=0.0)
        withdrawing = roll_forward_portfolio(lots, universe, prices, 0.025, withdrawing_fraction=1.0)
        assert len(accumulating["new_lots"]) == 1
        assert len(withdrawing["new_lots"]) == 0
        # The distribution itself is still counted (still taxed) either way -- only reinvestment stops.
        assert accumulating["distribution_by_account_and_type"] == withdrawing["distribution_by_account_and_type"]

    def test_tax_advantaged_accounts_always_reinvest_even_while_withdrawing(self):
        universe = _universe(
            VTI={"asset_class": "US", "nominal_return": 0.08, "expense_ratio": 0.0, "dividend_rate": 0.02, "income_type": "qualified"}
        )
        lots = [{"ticker": "VTI", "account_type": "Roth IRA", "shares": 100.0, "basis_per_share": 100.0}]
        prices = {"VTI": 100.0}
        result = roll_forward_portfolio(lots, universe, prices, 0.025, withdrawing_fraction=1.0)
        assert len(result["new_lots"]) == 1

    def test_missing_price_or_universe_entry_skipped_not_a_crash(self):
        universe = _universe(
            VTI={"asset_class": "US", "nominal_return": 0.08, "expense_ratio": 0.0, "dividend_rate": 0.02, "income_type": "qualified"}
        )
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 100.0, "basis_per_share": 100.0},
            {"ticker": "GHOST", "account_type": "Taxable", "shares": 50.0, "basis_per_share": 10.0},
        ]
        prices = {"VTI": 100.0}  # GHOST has no price
        result = roll_forward_portfolio(lots, universe, prices, 0.025, withdrawing_fraction=0.0)
        assert "GHOST" not in result["updated_prices_by_ticker"]
        assert "VTI" in result["updated_prices_by_ticker"]

    def test_reinvestment_lots_priced_at_start_of_year_price(self):
        universe = _universe(
            VTI={"asset_class": "US", "nominal_return": 0.08, "expense_ratio": 0.0, "dividend_rate": 0.02, "income_type": "qualified"}
        )
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 100.0, "basis_per_share": 100.0}]
        prices = {"VTI": 100.0}
        result = roll_forward_portfolio(lots, universe, prices, 0.025, withdrawing_fraction=0.0)
        new_lot = result["new_lots"][0]
        assert new_lot["basis_per_share"] == pytest.approx(100.0)  # start-of-year price, not the rolled-forward one
        assert new_lot["shares"] == pytest.approx(200.0 / 100.0)  # $200 distribution / $100 start price


# ---- sell_lots ----


class TestSellLots:
    def test_hifo_sells_smallest_gain_lot_first(self):
        # Same ticker, two lots at different basis -- HIFO must sell the higher-basis (smaller-gain)
        # lot first, which minimizes realized gain for the first dollars raised.
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "VTI", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 90.0, "year_acquired": 2021},
        ]
        prices = {"VTI": 100.0}
        result = sell_lots(lots, dollars_needed=500.0, prices_by_ticker=prices, current_year=2026, sale_method="hifo")
        # $500 raised entirely from the $90-basis lot (5 shares @ $100 = $500) -- the $50-basis lot
        # must be untouched.
        assert len(result["lots_sold"]) == 1
        sold = result["lots_sold"][0]
        assert sold["shares_sold"] == pytest.approx(5.0)
        assert sold["realized_gain"] == pytest.approx(5.0 * (100.0 - 90.0))
        remaining = result["remaining_lots"]
        assert len(remaining) == 2  # untouched $50-basis lot + leftover half of the $90-basis lot
        assert any(lot["basis_per_share"] == pytest.approx(50.0) and lot["shares"] == pytest.approx(10.0) for lot in remaining)

    def test_hifo_minimizes_gain_across_tickers_not_just_within_one(self):
        # Two DIFFERENT tickers -- HIFO must rank by gain-per-share using each ticker's OWN price,
        # not compare raw basis_per_share dollar amounts across tickers.
        lots = [
            {"ticker": "AAA", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 40.0, "year_acquired": 2020},  # gain/share = 60
            {"ticker": "BBB", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 5.0, "year_acquired": 2020},   # gain/share = 5
        ]
        prices = {"AAA": 100.0, "BBB": 10.0}
        result = sell_lots(lots, dollars_needed=50.0, prices_by_ticker=prices, current_year=2026, sale_method="hifo")
        assert result["lots_sold"][0]["ticker"] == "BBB"  # smallest gain-per-share, sold first

    def test_fifo_sells_oldest_lot_first(self):
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 50.0, "year_acquired": 2022},
            {"ticker": "VTI", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 90.0, "year_acquired": 2018},
        ]
        prices = {"VTI": 100.0}
        result = sell_lots(lots, dollars_needed=500.0, prices_by_ticker=prices, current_year=2026, sale_method="fifo")
        assert result["lots_sold"][0]["ticker"] == "VTI"
        assert result["lots_sold"][0]["basis"] == pytest.approx(5.0 * 90.0)  # the 2018 (oldest) lot's own basis

    def test_average_cost_blends_basis_within_ticker(self):
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 50.0, "year_acquired": 2018},
            {"ticker": "VTI", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 90.0, "year_acquired": 2022},
        ]
        prices = {"VTI": 100.0}
        # Blended average basis = (10*50 + 10*90) / 20 = 70. Selling 5 shares (oldest lot first, per
        # the module's own documented FIFO-within-average-cost convention) uses that blended basis.
        result = sell_lots(lots, dollars_needed=500.0, prices_by_ticker=prices, current_year=2026, sale_method="average_cost")
        sold = result["lots_sold"][0]
        assert sold["basis"] == pytest.approx(5.0 * 70.0)
        assert sold["holding_period_years"] == 2026 - 2018  # oldest lot's own acquisition year

    def test_partial_lot_sale_leaves_correctly_sized_remainder(self):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 50.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}
        result = sell_lots(lots, dollars_needed=300.0, prices_by_ticker=prices, current_year=2026)
        assert result["lots_sold"][0]["shares_sold"] == pytest.approx(3.0)
        assert result["remaining_lots"][0]["shares"] == pytest.approx(7.0)
        assert result["remaining_lots"][0]["basis_per_share"] == pytest.approx(50.0)  # unchanged

    def test_shortfall_when_lots_insufficient(self):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 5.0, "basis_per_share": 50.0, "year_acquired": 2020}]
        prices = {"VTI": 100.0}  # total sellable value = $500
        result = sell_lots(lots, dollars_needed=800.0, prices_by_ticker=prices, current_year=2026)
        assert result["total_proceeds"] == pytest.approx(500.0)
        assert result["shortfall"] == pytest.approx(300.0)
        assert result["remaining_lots"] == []  # everything sold, still short

    def test_short_term_vs_long_term_classification(self):
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 5.0, "basis_per_share": 50.0, "year_acquired": 2026},  # same year -> short-term
            {"ticker": "VTI", "account_type": "Taxable", "shares": 5.0, "basis_per_share": 50.0, "year_acquired": 2020},  # long-term
        ]
        prices = {"VTI": 100.0}
        result = sell_lots(lots, dollars_needed=1000.0, prices_by_ticker=prices, current_year=2026, sale_method="fifo")
        # FIFO sells the 2020 lot (oldest) first, then the 2026 lot.
        assert result["long_term_gain"] == pytest.approx(5.0 * (100.0 - 50.0))
        assert result["short_term_gain"] == pytest.approx(5.0 * (100.0 - 50.0))

    def test_lot_with_unknown_price_is_never_sold(self):
        lots = [
            {"ticker": "GHOST", "account_type": "Taxable", "shares": 100.0, "basis_per_share": 10.0, "year_acquired": 2020},
            {"ticker": "VTI", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 50.0, "year_acquired": 2020},
        ]
        prices = {"VTI": 100.0}  # GHOST has no price at all
        result = sell_lots(lots, dollars_needed=100.0, prices_by_ticker=prices, current_year=2026)
        assert all(s["ticker"] != "GHOST" for s in result["lots_sold"])
        assert any(lot["ticker"] == "GHOST" for lot in result["remaining_lots"])

    def test_zero_dollars_needed_sells_nothing(self):
        lots = [{"ticker": "VTI", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 50.0, "year_acquired": 2020}]
        result = sell_lots(lots, dollars_needed=0.0, prices_by_ticker={"VTI": 100.0}, current_year=2026)
        assert result["lots_sold"] == []
        assert result["remaining_lots"] == lots
        assert result["shortfall"] == 0.0

    def test_invalid_sale_method_raises(self):
        with pytest.raises(ValueError, match="sale_method"):
            sell_lots([], 100.0, {}, 2026, sale_method="lifo")

    def test_negative_dollars_needed_raises(self):
        with pytest.raises(ValueError, match="dollars_needed"):
            sell_lots([], -100.0, {}, 2026)

    def test_shares_conserved_sold_plus_remaining_equals_original(self):
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "VTI", "account_type": "Taxable", "shares": 7.0, "basis_per_share": 80.0, "year_acquired": 2022},
        ]
        prices = {"VTI": 100.0}
        result = sell_lots(lots, dollars_needed=650.0, prices_by_ticker=prices, current_year=2026, sale_method="fifo")
        shares_sold = sum(s["shares_sold"] for s in result["lots_sold"])
        shares_remaining = sum(lot["shares"] for lot in result["remaining_lots"])
        assert shares_sold + shares_remaining == pytest.approx(17.0)
        assert shares_sold >= 0
        assert all(lot["shares"] >= 0 for lot in result["remaining_lots"])


# ---- draw_order_fill ----


class TestDrawOrderFill:
    def _lots(self):
        return [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 10.0, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "VTI", "account_type": "Traditional 401(k)", "shares": 10.0, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "VTI", "account_type": "Roth IRA", "shares": 10.0, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "VTI", "account_type": "HSA", "shares": 10.0, "basis_per_share": 50.0, "year_acquired": 2020},
        ]

    def test_default_order_draws_taxable_first(self):
        result = draw_order_fill(500.0, self._lots(), {"VTI": 100.0}, 2026)
        assert "Taxable" in result["sales_by_account_type"]
        assert "Traditional 401(k)" not in result["sales_by_account_type"]
        assert "Roth IRA" not in result["sales_by_account_type"]

    def test_moves_to_next_account_once_first_is_exhausted(self):
        # Taxable only has $1,000 of value (10 shares @ $100) -- asking for $1,500 must spill into
        # Traditional 401(k) next, per DEFAULT_DRAW_ORDER.
        result = draw_order_fill(1500.0, self._lots(), {"VTI": 100.0}, 2026)
        assert result["sales_by_account_type"]["Taxable"]["total_proceeds"] == pytest.approx(1000.0)
        assert "Traditional 401(k)" in result["sales_by_account_type"]
        assert result["sales_by_account_type"]["Traditional 401(k)"]["total_proceeds"] == pytest.approx(500.0)
        assert result["unfunded_shortfall"] == pytest.approx(0.0)

    def test_hsa_excluded_by_default(self):
        # Ask for far more than Taxable+Traditional+Roth combined ($3,000) -- HSA's own $1,000 must
        # NEVER be touched even though it would fully cover the remainder.
        result = draw_order_fill(10000.0, self._lots(), {"VTI": 100.0}, 2026)
        assert "HSA" not in result["sales_by_account_type"]
        assert any(lot["account_type"] == "HSA" and lot["shares"] == pytest.approx(10.0) for lot in result["remaining_lots"])
        assert result["unfunded_shortfall"] == pytest.approx(10000.0 - 3000.0)

    def test_hsa_included_when_explicitly_opted_in(self):
        custom_order = DEFAULT_DRAW_ORDER + ["HSA"]
        result = draw_order_fill(10000.0, self._lots(), {"VTI": 100.0}, 2026, draw_order=custom_order)
        assert "HSA" in result["sales_by_account_type"]
        assert result["unfunded_shortfall"] == pytest.approx(10000.0 - 4000.0)

    def test_zero_dollars_needed_touches_nothing(self):
        lots = self._lots()
        result = draw_order_fill(0.0, lots, {"VTI": 100.0}, 2026)
        assert result["sales_by_account_type"] == {}
        assert result["total_proceeds"] == 0.0
        assert len(result["remaining_lots"]) == len(lots)

    def test_custom_draw_order_respected(self):
        result = draw_order_fill(500.0, self._lots(), {"VTI": 100.0}, 2026, draw_order=["Roth IRA", "Taxable"])
        assert "Roth IRA" in result["sales_by_account_type"]
        assert "Taxable" not in result["sales_by_account_type"]

    def test_share_count_conserved_across_the_whole_ledger(self):
        lots = self._lots()
        total_shares_before = sum(lot["shares"] for lot in lots)
        result = draw_order_fill(1500.0, lots, {"VTI": 100.0}, 2026)
        shares_sold = sum(s["shares_sold"] for r in result["sales_by_account_type"].values() for s in r["lots_sold"])
        shares_remaining = sum(lot["shares"] for lot in result["remaining_lots"])
        assert shares_sold + shares_remaining == pytest.approx(total_shares_before)

    def test_negative_dollars_needed_raises(self):
        with pytest.raises(ValueError, match="dollars_needed"):
            draw_order_fill(-100.0, [], {}, 2026)


# ---- bracket_aware_draw (MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 1, 2026-08-23) ----


class TestBracketAwareDraw:
    def _three_account_lots(self):
        # $50,000 in each of Traditional/Taxable/Roth -- the caller (modules/projection.py) is
        # responsible for sizing the three targets against each bucket's own bracket headroom/
        # balance; this function only executes whatever three targets it's handed.
        return [
            {"ticker": "VTI", "account_type": "Traditional 401(k)", "shares": 500.0, "basis_per_share": 100.0, "year_acquired": 2020},
            {"ticker": "VTI", "account_type": "Taxable", "shares": 500.0, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "VTI", "account_type": "Roth IRA", "shares": 500.0, "basis_per_share": 100.0, "year_acquired": 2020},
        ]

    def test_traditional_capped_at_its_own_target_even_with_more_available(self):
        # Traditional holds $50,000 but the caller only asked for $10,000 (its own bracket-ceiling
        # decision) -- bracket_aware_draw must respect that target, not sell more just because more
        # is available.
        result = bracket_aware_draw(10000.0, 0.0, 0.0, self._three_account_lots(), {"VTI": 100.0}, 2026)
        assert result["sales_by_account_type"]["Traditional 401(k)"]["total_proceeds"] == pytest.approx(10000.0)
        assert "Taxable" not in result["sales_by_account_type"]
        assert "Roth IRA" not in result["sales_by_account_type"]
        assert result["unfunded_shortfall"] == pytest.approx(0.0)

    def test_roth_only_touched_once_taxable_target_is_set(self):
        result = bracket_aware_draw(0.0, 20000.0, 5000.0, self._three_account_lots(), {"VTI": 100.0}, 2026)
        assert "Traditional 401(k)" not in result["sales_by_account_type"]
        assert result["sales_by_account_type"]["Taxable"]["total_proceeds"] == pytest.approx(20000.0)
        assert result["sales_by_account_type"]["Roth IRA"]["total_proceeds"] == pytest.approx(5000.0)

    def test_all_three_buckets_fill_independently_in_one_call(self):
        result = bracket_aware_draw(10000.0, 15000.0, 5000.0, self._three_account_lots(), {"VTI": 100.0}, 2026)
        assert result["total_proceeds"] == pytest.approx(30000.0)
        assert result["unfunded_shortfall"] == pytest.approx(0.0)
        assert result["unfunded_shortfall_by_bucket"] == {"traditional": pytest.approx(0.0), "taxable": pytest.approx(0.0), "roth": pytest.approx(0.0)}

    def test_shortfall_reported_per_bucket_not_just_in_aggregate(self):
        # Roth only has $50,000 -- asking for $60,000 there specifically must show up as a ROTH
        # shortfall, not smeared across the total.
        result = bracket_aware_draw(0.0, 0.0, 60000.0, self._three_account_lots(), {"VTI": 100.0}, 2026)
        assert result["unfunded_shortfall_by_bucket"]["roth"] == pytest.approx(10000.0)
        assert result["unfunded_shortfall_by_bucket"]["traditional"] == pytest.approx(0.0)
        assert result["unfunded_shortfall_by_bucket"]["taxable"] == pytest.approx(0.0)
        assert result["unfunded_shortfall"] == pytest.approx(10000.0)

    def test_hsa_and_other_never_touched(self):
        lots = self._three_account_lots() + [
            {"ticker": "VTI", "account_type": "HSA", "shares": 500.0, "basis_per_share": 100.0, "year_acquired": 2020},
            {"ticker": "VTI", "account_type": "Other", "shares": 500.0, "basis_per_share": 100.0, "year_acquired": 2020},
        ]
        result = bracket_aware_draw(10000.0, 10000.0, 10000.0, lots, {"VTI": 100.0}, 2026)
        assert "HSA" not in result["sales_by_account_type"]
        assert "Other" not in result["sales_by_account_type"]
        assert any(lot["account_type"] == "HSA" for lot in result["remaining_lots"])
        assert any(lot["account_type"] == "Other" for lot in result["remaining_lots"])

    def test_zero_everywhere_touches_nothing(self):
        lots = self._three_account_lots()
        result = bracket_aware_draw(0.0, 0.0, 0.0, lots, {"VTI": 100.0}, 2026)
        assert result["sales_by_account_type"] == {}
        assert result["total_proceeds"] == pytest.approx(0.0)
        assert len(result["remaining_lots"]) == len(lots)


# ---- rebalance_account ----


class TestRebalanceAccount:
    def test_corrects_drifted_tickers_to_exact_target(self):
        # AAA has drifted to 80% of the account; target is 50/50.
        lots = [
            {"ticker": "AAA", "account_type": "Roth IRA", "shares": 8.0, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "BBB", "account_type": "Roth IRA", "shares": 2.0, "basis_per_share": 50.0, "year_acquired": 2020},
        ]
        prices = {"AAA": 100.0, "BBB": 100.0}  # $800 AAA, $200 BBB -> 80/20
        result = rebalance_account(lots, "Roth IRA", {"AAA": 0.5, "BBB": 0.5}, prices, 2026)
        by_ticker = {}
        for lot in result["rebalanced_lots"]:
            by_ticker[lot["ticker"]] = by_ticker.get(lot["ticker"], 0.0) + lot["shares"]
        total_value = sum(shares * prices[t] for t, shares in by_ticker.items())
        assert by_ticker["AAA"] * prices["AAA"] / total_value == pytest.approx(0.5, abs=1e-6)
        assert by_ticker["BBB"] * prices["BBB"] / total_value == pytest.approx(0.5, abs=1e-6)
        assert result["residual_drift"] == pytest.approx(0.0)

    def test_transfers_sum_to_zero(self):
        lots = [
            {"ticker": "AAA", "account_type": "Roth IRA", "shares": 8.0, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "BBB", "account_type": "Roth IRA", "shares": 2.0, "basis_per_share": 50.0, "year_acquired": 2020},
        ]
        prices = {"AAA": 100.0, "BBB": 100.0}
        result = rebalance_account(lots, "Roth IRA", {"AAA": 0.5, "BBB": 0.5}, prices, 2026)
        assert sum(result["transfers_by_ticker"].values()) == pytest.approx(0.0, abs=1e-6)

    def test_total_value_conserved_no_cash_created_or_destroyed(self):
        lots = [
            {"ticker": "AAA", "account_type": "Roth IRA", "shares": 8.0, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "BBB", "account_type": "Roth IRA", "shares": 2.0, "basis_per_share": 50.0, "year_acquired": 2020},
        ]
        prices = {"AAA": 100.0, "BBB": 100.0}
        value_before = sum(lot["shares"] * prices[lot["ticker"]] for lot in lots)
        result = rebalance_account(lots, "Roth IRA", {"AAA": 0.5, "BBB": 0.5}, prices, 2026)
        value_after = sum(lot["shares"] * prices[lot["ticker"]] for lot in result["rebalanced_lots"])
        assert value_after == pytest.approx(value_before)

    def test_no_op_when_no_target_configured(self):
        lots = [{"ticker": "AAA", "account_type": "Roth IRA", "shares": 8.0, "basis_per_share": 50.0, "year_acquired": 2020}]
        prices = {"AAA": 100.0}
        result = rebalance_account(lots, "Roth IRA", {}, prices, 2026)
        assert result["rebalanced_lots"] == lots
        assert result["residual_drift"] == pytest.approx(0.0)
        assert result["transfers_by_ticker"] == {}

    def test_rebalance_band_leaves_small_drift_untouched(self):
        # AAA at 55%, target 50% -- a 10% band should leave it alone (deviation 0.05 <= band).
        lots = [
            {"ticker": "AAA", "account_type": "Roth IRA", "shares": 5.5, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "BBB", "account_type": "Roth IRA", "shares": 4.5, "basis_per_share": 50.0, "year_acquired": 2020},
        ]
        prices = {"AAA": 100.0, "BBB": 100.0}
        result = rebalance_account(lots, "Roth IRA", {"AAA": 0.5, "BBB": 0.5}, prices, 2026, rebalance_band=0.10)
        assert result["transfers_by_ticker"] == {}
        assert result["residual_drift"] == pytest.approx(0.05, abs=1e-6)

    def test_untradeable_ticker_left_alone_and_counted_in_residual_drift(self):
        lots = [{"ticker": "GHOST", "account_type": "Roth IRA", "shares": 10.0, "basis_per_share": 50.0, "year_acquired": 2020}]
        prices = {}  # GHOST has no price at all
        result = rebalance_account(lots, "Roth IRA", {"GHOST": 1.0}, prices, 2026)
        assert result["rebalanced_lots"] == lots
        assert result["residual_drift"] > 0

    def test_buys_a_ticker_not_previously_held(self):
        lots = [{"ticker": "AAA", "account_type": "Roth IRA", "shares": 10.0, "basis_per_share": 50.0, "year_acquired": 2020}]
        prices = {"AAA": 100.0, "BBB": 100.0}
        result = rebalance_account(lots, "Roth IRA", {"AAA": 0.5, "BBB": 0.5}, prices, 2026)
        bbb_lots = [lot for lot in result["rebalanced_lots"] if lot["ticker"] == "BBB"]
        assert bbb_lots and bbb_lots[0]["shares"] > 0
        assert bbb_lots[0]["basis_per_share"] == pytest.approx(100.0)
        assert bbb_lots[0]["account_type"] == "Roth IRA"
        assert bbb_lots[0]["year_acquired"] == 2026

    def test_sells_a_ticker_to_zero_when_target_weight_is_zero(self):
        lots = [
            {"ticker": "AAA", "account_type": "Roth IRA", "shares": 5.0, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "BBB", "account_type": "Roth IRA", "shares": 5.0, "basis_per_share": 50.0, "year_acquired": 2020},
        ]
        prices = {"AAA": 100.0, "BBB": 100.0}
        result = rebalance_account(lots, "Roth IRA", {"AAA": 1.0, "BBB": 0.0}, prices, 2026)
        assert all(lot["ticker"] != "BBB" for lot in result["rebalanced_lots"])

    def test_multiple_lots_of_same_ticker_scaled_proportionally_not_blended(self):
        lots = [
            {"ticker": "AAA", "account_type": "Roth IRA", "shares": 4.0, "basis_per_share": 40.0, "year_acquired": 2018},
            {"ticker": "AAA", "account_type": "Roth IRA", "shares": 4.0, "basis_per_share": 60.0, "year_acquired": 2022},
            {"ticker": "BBB", "account_type": "Roth IRA", "shares": 2.0, "basis_per_share": 50.0, "year_acquired": 2020},
        ]
        prices = {"AAA": 100.0, "BBB": 100.0}  # $800 AAA (80%), $200 BBB (20%) -- target 50/50
        result = rebalance_account(lots, "Roth IRA", {"AAA": 0.5, "BBB": 0.5}, prices, 2026)
        aaa_lots = sorted((lot for lot in result["rebalanced_lots"] if lot["ticker"] == "AAA"), key=lambda l: l["year_acquired"])
        assert len(aaa_lots) == 2  # scaled down, not merged into one blended lot
        # Both original lots had equal shares (4.0 each) -- after uniform scaling they must still
        # be equal to each other.
        assert aaa_lots[0]["shares"] == pytest.approx(aaa_lots[1]["shares"])
        assert aaa_lots[0]["basis_per_share"] == pytest.approx(40.0)  # basis never changes, only shares
        assert aaa_lots[1]["basis_per_share"] == pytest.approx(60.0)

    def test_rebalanceable_account_types_excludes_taxable_and_other(self):
        assert "Taxable" not in REBALANCEABLE_ACCOUNT_TYPES
        assert "Other" not in REBALANCEABLE_ACCOUNT_TYPES
        assert "Traditional 401(k)" in REBALANCEABLE_ACCOUNT_TYPES


# ---- annual_withdrawal_target ----


class TestAnnualWithdrawalTarget:
    def test_flat_percentage_computes_rate_times_portfolio_value(self):
        result = annual_withdrawal_target("flat_percentage", {"rate": 0.04}, {"portfolio_value": 1_000_000.0})
        assert result == pytest.approx(40000.0)

    def test_flat_percentage_default_rate_matches_spec(self):
        # MODEL_WIRING.md §2.3's own stated default.
        result = annual_withdrawal_target("flat_percentage", {"rate": 0.04}, {"portfolio_value": 500000.0})
        assert result == pytest.approx(20000.0)

    def test_zero_portfolio_value_produces_zero_target(self):
        result = annual_withdrawal_target("flat_percentage", {"rate": 0.04}, {"portfolio_value": 0.0})
        assert result == pytest.approx(0.0)

    def test_negative_portfolio_value_clamped_to_zero(self):
        # Shouldn't occur in practice, but defensively never a negative withdrawal target.
        result = annual_withdrawal_target("flat_percentage", {"rate": 0.04}, {"portfolio_value": -1000.0})
        assert result == pytest.approx(0.0)

    def test_extra_unused_state_keys_are_accepted_not_rejected(self):
        # state carries the union of what ANY strategy might need -- flat_percentage only reads
        # portfolio_value, but extra keys (for future strategies) must not cause an error.
        result = annual_withdrawal_target(
            "flat_percentage",
            {"rate": 0.04},
            {"portfolio_value": 100000.0, "prior_year_withdrawal": 5000.0, "years_elapsed": 3, "spending_need": 50000.0},
        )
        assert result == pytest.approx(4000.0)

    def test_unimplemented_strategy_raises_not_implemented_not_silent_fallback(self):
        with pytest.raises(NotImplementedError, match="guardrails"):
            annual_withdrawal_target("guardrails", {}, {"portfolio_value": 100000.0})

    def test_withdrawal_strategies_contains_flat_percentage_and_target_net_spending(self):
        assert WITHDRAWAL_STRATEGIES == ("flat_percentage", "target_net_spending")

    def test_target_net_spending_returns_spending_need_as_initial_guess(self):
        # MODULE_G2_STEP7_BRACKET_AWARE_WITHDRAWALS.md Part 2 — this function only returns the
        # naive starting guess; the actual fixed-point solve lives in modules/projection.py.
        result = annual_withdrawal_target(
            "target_net_spending", {}, {"portfolio_value": 1_000_000.0, "spending_need": 62000.0}
        )
        assert result == pytest.approx(62000.0)

    def test_target_net_spending_missing_spending_need_defaults_to_zero(self):
        result = annual_withdrawal_target("target_net_spending", {}, {"portfolio_value": 1_000_000.0})
        assert result == pytest.approx(0.0)

    def test_target_net_spending_negative_spending_need_clamped_to_zero(self):
        result = annual_withdrawal_target(
            "target_net_spending", {}, {"portfolio_value": 1_000_000.0, "spending_need": -500.0}
        )
        assert result == pytest.approx(0.0)
