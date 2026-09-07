import pytest

from modules.portfolio import (
    ACCOUNT_TYPES,
    build_universe,
    etf_lookup,
    expense_adjusted_return,
    group_positions_by_account,
    holding_cost_basis_value,
    holding_gross_value,
    load_asset_classes,
    portfolio_summary,
    portfolio_value_by_account_type,
    real_return,
    summarize_holdings,
    target_allocation_blended_return,
    target_allocation_blended_weights,
)

# Expense ratios here deliberately match what the pre-amendment tests used inline on each holding,
# so most of those tests' expected numbers didn't need to change — only where the ER now comes from
# (etf_lookup(universe), not the holding dict) did.
TEST_UNIVERSE = {
    "asset_classes": {
        "US": {"label": "US Total Market", "nominal_return": 0.08},
        "BOND": {"label": "Bonds", "nominal_return": 0.04},
    },
    "etfs": {
        "VTI": {"asset_class": "US", "expense_ratio": 0.0003, "dividend_rate": 0.013, "income_type": "qualified"},
        "BND": {"asset_class": "BOND", "expense_ratio": 0.0005, "dividend_rate": 0.03, "income_type": "ordinary"},
    },
}

def _universe_with_er(ticker: str, expense_ratio: float) -> dict:
    """A one-off universe for tests that need a specific, round expense-ratio value on `ticker`."""
    universe = {
        "asset_classes": TEST_UNIVERSE["asset_classes"],
        "etfs": {t: dict(info) for t, info in TEST_UNIVERSE["etfs"].items()},
    }
    universe["etfs"][ticker] = {**universe["etfs"][ticker], "expense_ratio": expense_ratio}
    return universe


def test_load_asset_classes_seed_file_has_expected_shape():
    asset_classes = load_asset_classes()
    assert "US" in asset_classes
    assert asset_classes["US"]["label"] == "US Total Market"
    assert asset_classes["US"]["nominal_return"] > 0


def test_load_asset_classes_no_longer_has_gbp_specific_class():
    # Dropped per Step 1: non-USD support removed alongside the retired CASH_GBP special-casing.
    assert "GBP_INT" not in load_asset_classes()


def test_build_universe_combines_classes_and_ticker_assignments():
    asset_classes = {"US": {"label": "US Total Market", "nominal_return": 0.065}}
    ticker_universe = {
        "VTI": {"asset_class": "US", "expense_ratio": 0.0003, "dividend_rate": 0.013, "income_type": "qualified"},
        "VOO": {"asset_class": "US", "expense_ratio": 0.0003, "dividend_rate": 0.012, "income_type": "ordinary"},
    }
    universe = build_universe(asset_classes, ticker_universe)
    assert universe["asset_classes"] == asset_classes
    assert universe["etfs"] == {
        "VTI": {"asset_class": "US", "expense_ratio": 0.0003, "dividend_rate": 0.013, "income_type": "qualified"},
        "VOO": {"asset_class": "US", "expense_ratio": 0.0003, "dividend_rate": 0.012, "income_type": "ordinary"},
    }


def test_build_universe_defaults_missing_expense_ratio_and_dividend_fields():
    # A ticker_universe entry with only "asset_class" (e.g. mid-migration from an older save
    # shape) must not crash — expense ratio and dividend rate default to 0.0, dividend type to
    # "qualified", rather than raising a KeyError.
    universe = build_universe({"US": {"label": "US", "nominal_return": 0.065}}, {"VTI": {"asset_class": "US"}})
    assert universe["etfs"]["VTI"] == {
        "asset_class": "US",
        "expense_ratio": 0.0,
        "dividend_rate": 0.0,
        "income_type": "qualified",
    }


def test_etf_lookup_returns_asset_class_nominal_return_and_user_entered_fields():
    info = etf_lookup("VTI", TEST_UNIVERSE)
    assert info["asset_class"] == "US"
    assert info["asset_class_label"] == "US Total Market"
    assert info["nominal_return"] == 0.08
    assert info["expense_ratio"] == 0.0003
    assert info["dividend_rate"] == 0.013
    assert info["income_type"] == "qualified"


def test_build_universe_accepts_interest_income_type():
    # MODEL_WIRING.md §4.2 (2026-08-10): income_type gained a third option, "interest", for
    # cash/money-market/short-duration-bond tickers (e.g. SPAXX, SGOV) — not special-cased
    # anywhere, just another valid string value flowing straight through like "qualified"/"ordinary".
    asset_classes = {"USD_CASH": {"label": "Cash (USD)", "nominal_return": 0.0}}
    ticker_universe = {
        "SGOV": {"asset_class": "USD_CASH", "expense_ratio": 0.001, "dividend_rate": 0.04, "income_type": "interest"}
    }
    universe = build_universe(asset_classes, ticker_universe)
    assert universe["etfs"]["SGOV"]["income_type"] == "interest"
    assert etf_lookup("SGOV", universe)["income_type"] == "interest"


def test_real_return_matches_fisher_equation():
    assert real_return(0.065, 0.025) == pytest.approx(1.065 / 1.025 - 1)


def test_real_return_zero_inflation_equals_nominal():
    assert real_return(0.07, 0.0) == pytest.approx(0.07)


def test_expense_adjusted_return_matches_geometric_formula():
    # R_net = (1 + R_gross) / (1 + ER) - 1
    assert expense_adjusted_return(0.08, 0.0025) == pytest.approx(1.08 / 1.0025 - 1)


def test_expense_adjusted_return_zero_expense_ratio_equals_gross():
    assert expense_adjusted_return(0.065, 0.0) == pytest.approx(0.065)


def test_expense_adjusted_return_matches_alternate_form():
    # (1+R)/(1+ER) - 1 == (R-ER)/(1+ER) — same formula, two algebraic forms
    gross, er = 0.08, 0.0025
    assert expense_adjusted_return(gross, er) == pytest.approx((gross - er) / (1 + er))


def test_holding_gross_value():
    assert holding_gross_value(shares=10, price=250.0) == 2500.0


def test_holding_cost_basis_value():
    assert holding_cost_basis_value(shares=10, cost_basis_per_share=80.0) == 800.0


def test_summarize_holdings_blends_by_gross_value():
    holdings = [
        {"ticker": "VTI", "shares": 10, "price": 100.0, "cost_basis_per_share": 100.0, "account_type": "Taxable"},
        {"ticker": "BND", "shares": 10, "price": 100.0, "cost_basis_per_share": 100.0, "account_type": "Taxable"},
    ]
    summary = summarize_holdings(holdings, TEST_UNIVERSE, inflation_rate=0.0)
    assert summary["total_gross_value"] == 2000.0
    assert summary["blended_nominal_return"] == pytest.approx(0.06)
    # VTI 0.0003 + BND 0.0005, equal-weighted -> 0.0004
    assert summary["blended_expense_ratio"] == pytest.approx(0.0004)
    assert summary["value_by_asset_class"] == {"US": 1000.0, "BOND": 1000.0}


def test_summarize_holdings_real_return_nets_expense_ratio_then_inflation():
    # VTI: 8% nominal, 0.30% ER (universe-level, not a holding field) -> net-of-fee =
    # (1.08/1.003) - 1; then Fisher-deflate by 2% inflation.
    universe = _universe_with_er("VTI", 0.003)
    holdings = [{"ticker": "VTI", "shares": 10, "price": 100.0, "cost_basis_per_share": 100.0, "account_type": "Taxable"}]
    summary = summarize_holdings(holdings, universe, inflation_rate=0.02)
    net_of_fee = expense_adjusted_return(0.08, 0.003)
    expected_real = real_return(net_of_fee, 0.02)
    assert summary["blended_real_return"] == pytest.approx(expected_real)
    # And blended_nominal_return stays the pure gross asset-class figure, unaffected by fees:
    assert summary["blended_nominal_return"] == pytest.approx(0.08)


def test_summarize_holdings_real_return_lower_than_naive_fisher_when_fees_present():
    # Sanity check: with any positive expense ratio, real return must be strictly lower than the
    # old (pre-fix) calculation that ignored fees entirely.
    universe = _universe_with_er("VTI", 0.01)
    holdings = [{"ticker": "VTI", "shares": 10, "price": 100.0, "cost_basis_per_share": 100.0, "account_type": "Taxable"}]
    summary = summarize_holdings(holdings, universe, inflation_rate=0.025)
    naive_real_return_ignoring_fees = real_return(0.08, 0.025)
    assert summary["blended_real_return"] < naive_real_return_ignoring_fees


def test_summarize_holdings_zero_expense_ratio_ticker():
    universe = _universe_with_er("VTI", 0.0)
    holdings = [{"ticker": "VTI", "shares": 10, "price": 100.0, "cost_basis_per_share": 100.0, "account_type": "Taxable"}]
    summary = summarize_holdings(holdings, universe, inflation_rate=0.0)
    assert summary["blended_expense_ratio"] == 0.0
    assert summary["blended_real_return"] == pytest.approx(0.08)  # no fee, no inflation -> nominal unchanged


def test_summarize_holdings_empty():
    summary = summarize_holdings([], TEST_UNIVERSE, inflation_rate=0.025)
    assert summary["total_gross_value"] == 0.0
    assert summary["value_by_asset_class"] == {}


def test_portfolio_summary_by_account_and_overall():
    accounts = [
        {
            "name": "401k",
            "type": "Traditional 401(k)",
            "holdings": [{"ticker": "BND", "shares": 10, "price": 100.0, "cost_basis_per_share": 100.0}],
        },
        {
            "name": "Brokerage",
            "type": "Taxable",
            "holdings": [{"ticker": "VTI", "shares": 10, "price": 100.0, "cost_basis_per_share": 80.0}],
        },
        {
            "name": "Roth IRA",
            "type": "Roth IRA",
            "holdings": [{"ticker": "VTI", "shares": 10, "price": 100.0, "cost_basis_per_share": 10.0}],
        },
    ]
    result = portfolio_summary(accounts, TEST_UNIVERSE, inflation_rate=0.0)

    assert result["by_account"]["401k"]["total_gross_value"] == 1000.0
    assert result["by_account"]["Brokerage"]["total_gross_value"] == 1000.0
    assert result["by_account"]["Roth IRA"]["total_gross_value"] == 1000.0

    overall = result["overall"]
    assert overall["total_gross_value"] == 3000.0
    # weighted by gross value: (1000*0.04 BND + 1000*0.08 VTI + 1000*0.08 VTI) / 3000
    assert overall["blended_nominal_return"] == pytest.approx((0.04 + 0.08 + 0.08) / 3)


def test_group_positions_by_account_groups_and_selects_holding_fields():
    accounts = [{"name": "Brokerage", "type": "Taxable"}, {"name": "401k", "type": "Traditional 401(k)"}]
    positions = [
        {"account": "Brokerage", "ticker": "VTI", "shares": 10.0, "price": 250.0, "cost_basis_per_share": 200.0},
        {"account": "401k", "ticker": "BND", "shares": 5.0, "price": 80.0, "cost_basis_per_share": 75.0},
    ]
    result = group_positions_by_account(accounts, positions)

    assert result == [
        {
            "name": "Brokerage",
            "type": "Taxable",
            "holdings": [{"ticker": "VTI", "shares": 10.0, "price": 250.0, "cost_basis_per_share": 200.0}],
        },
        {
            "name": "401k",
            "type": "Traditional 401(k)",
            "holdings": [{"ticker": "BND", "shares": 5.0, "price": 80.0, "cost_basis_per_share": 75.0}],
        },
    ]


def test_group_positions_by_account_empty_account_gets_empty_holdings():
    accounts = [{"name": "Empty", "type": "Taxable"}]
    assert group_positions_by_account(accounts, []) == [{"name": "Empty", "type": "Taxable", "holdings": []}]


def test_group_positions_by_account_ignores_extra_position_fields():
    # A position dict might carry an "account" key (used for grouping) that isn't part of a
    # holding — group_positions_by_account must not leak it into the output.
    accounts = [{"name": "Brokerage", "type": "Taxable"}]
    positions = [{"account": "Brokerage", "ticker": "VTI", "shares": 1.0, "price": 1.0, "cost_basis_per_share": 1.0}]
    result = group_positions_by_account(accounts, positions)
    assert "account" not in result[0]["holdings"][0]


class TestTargetAllocationBlendedReturn:
    """2026-08-15, user request — the forward-looking counterpart to `summarize_holdings`'
    `blended_real_return`: what the portfolio WOULD earn if rebalanced to its own configured
    target allocation, not what it earns today."""

    def test_matches_summarize_holdings_when_target_equals_current_single_ticker(self):
        # Whole portfolio in VTI, target allocation also 100% VTI -- should reduce to VTI's own
        # real return, matching what summarize_holdings would compute for an all-VTI portfolio.
        result = target_allocation_blended_return(
            target_allocations={"Taxable": {"VTI": 1.0}},
            value_by_account_type={"Taxable": 100000.0},
            universe=TEST_UNIVERSE,
            inflation_rate=0.03,
        )
        expected_real = real_return(expense_adjusted_return(0.08, 0.0003), 0.03)
        assert result["blended_real_return"] == pytest.approx(expected_real)
        assert result["covered_value"] == pytest.approx(100000.0)
        assert result["total_value"] == pytest.approx(100000.0)

    def test_blends_two_tickers_by_normalized_weight_within_one_account_type(self):
        # Weights don't sum to 1.0 (0.5 + 0.25 = 0.75) -- must be normalized before blending, same
        # tolerance the target-allocation editor itself allows.
        result = target_allocation_blended_return(
            target_allocations={"Taxable": {"VTI": 0.5, "BND": 0.25}},
            value_by_account_type={"Taxable": 10000.0},
            universe=TEST_UNIVERSE,
            inflation_rate=0.03,
        )
        vti_real = real_return(expense_adjusted_return(0.08, 0.0003), 0.03)
        bnd_real = real_return(expense_adjusted_return(0.04, 0.0005), 0.03)
        # normalized: VTI gets 0.5/0.75 = 2/3, BND gets 0.25/0.75 = 1/3
        expected = (2 / 3) * vti_real + (1 / 3) * bnd_real
        assert result["blended_real_return"] == pytest.approx(expected)

    def test_weights_by_each_account_types_own_current_value(self):
        # $90k in a Taxable account targeting all-VTI, $10k in a Traditional 401(k) targeting
        # all-BND -- blend should be 90% VTI's return + 10% BND's return, not an even split.
        result = target_allocation_blended_return(
            target_allocations={"Taxable": {"VTI": 1.0}, "Traditional 401(k)": {"BND": 1.0}},
            value_by_account_type={"Taxable": 90000.0, "Traditional 401(k)": 10000.0},
            universe=TEST_UNIVERSE,
            inflation_rate=0.03,
        )
        vti_real = real_return(expense_adjusted_return(0.08, 0.0003), 0.03)
        bnd_real = real_return(expense_adjusted_return(0.04, 0.0005), 0.03)
        expected = 0.9 * vti_real + 0.1 * bnd_real
        assert result["blended_real_return"] == pytest.approx(expected)
        assert result["covered_value"] == pytest.approx(100000.0)

    def test_account_type_with_value_but_no_target_is_excluded_not_guessed(self):
        # $50k in Taxable has a target; $50k in a Roth IRA has NONE configured -- only the covered
        # $50k should be reflected, with covered_value/total_value exposing the gap.
        result = target_allocation_blended_return(
            target_allocations={"Taxable": {"VTI": 1.0}},
            value_by_account_type={"Taxable": 50000.0, "Roth IRA": 50000.0},
            universe=TEST_UNIVERSE,
            inflation_rate=0.03,
        )
        vti_real = real_return(expense_adjusted_return(0.08, 0.0003), 0.03)
        assert result["blended_real_return"] == pytest.approx(vti_real)
        assert result["covered_value"] == pytest.approx(50000.0)
        assert result["total_value"] == pytest.approx(100000.0)

    def test_nothing_covered_returns_all_zero(self):
        result = target_allocation_blended_return(
            target_allocations={},
            value_by_account_type={"Taxable": 50000.0},
            universe=TEST_UNIVERSE,
            inflation_rate=0.03,
        )
        assert result["blended_real_return"] == 0.0
        assert result["covered_value"] == 0.0
        assert result["total_value"] == pytest.approx(50000.0)

    def test_ticker_missing_from_universe_is_skipped_not_a_crash(self):
        result = target_allocation_blended_return(
            target_allocations={"Taxable": {"VTI": 0.5, "GHOST": 0.5}},
            value_by_account_type={"Taxable": 10000.0},
            universe=TEST_UNIVERSE,
            inflation_rate=0.03,
        )
        # GHOST isn't in the universe -- only VTI's own weight (normalized against the FULL 1.0
        # nominal weight sum, GHOST included) contributes, so this isn't silently rescaled up to
        # 100% VTI either.
        vti_real = real_return(expense_adjusted_return(0.08, 0.0003), 0.03)
        assert result["blended_real_return"] == pytest.approx(0.5 * vti_real)


class TestTargetAllocationBlendedWeights:
    """2026-08-30, user request (Portfolio tab "Target allocation by account type" section) — the
    companion to TestTargetAllocationBlendedReturn above: blends target WEIGHT by asset class
    (e.g. '62% US / 24% intl / 14% bonds') instead of blending target RETURN. Same coverage
    semantics throughout."""

    def test_single_account_type_single_ticker_is_100_percent_that_asset_class(self):
        result = target_allocation_blended_weights(
            target_allocations={"Taxable": {"VTI": 1.0}},
            value_by_account_type={"Taxable": 100000.0},
            universe=TEST_UNIVERSE,
        )
        assert result["weights_by_asset_class"] == pytest.approx({"US": 1.0})
        assert result["covered_value"] == pytest.approx(100000.0)
        assert result["total_value"] == pytest.approx(100000.0)

    def test_blends_two_tickers_by_normalized_weight_within_one_account_type(self):
        # Weights don't sum to 1.0 (0.5 + 0.25 = 0.75) -- must be normalized first, same tolerance
        # the target-allocation editor itself allows.
        result = target_allocation_blended_weights(
            target_allocations={"Taxable": {"VTI": 0.5, "BND": 0.25}},
            value_by_account_type={"Taxable": 10000.0},
            universe=TEST_UNIVERSE,
        )
        # normalized: VTI (US) gets 0.5/0.75 = 2/3, BND (BOND) gets 0.25/0.75 = 1/3
        assert result["weights_by_asset_class"] == pytest.approx({"US": 2 / 3, "BOND": 1 / 3})

    def test_weights_by_each_account_types_own_current_value(self):
        # $90k Taxable targeting all-VTI (US), $10k Traditional 401(k) targeting all-BND (BOND) --
        # blend should be 90% US / 10% BOND, not an even split.
        result = target_allocation_blended_weights(
            target_allocations={"Taxable": {"VTI": 1.0}, "Traditional 401(k)": {"BND": 1.0}},
            value_by_account_type={"Taxable": 90000.0, "Traditional 401(k)": 10000.0},
            universe=TEST_UNIVERSE,
        )
        assert result["weights_by_asset_class"] == pytest.approx({"US": 0.9, "BOND": 0.1})
        assert result["covered_value"] == pytest.approx(100000.0)

    def test_asset_classes_shared_across_tickers_are_summed(self):
        # Two different tickers, both mapped to the SAME asset class -- their weights must combine
        # into one entry, not overwrite each other.
        universe = {
            "asset_classes": TEST_UNIVERSE["asset_classes"],
            "etfs": {
                **TEST_UNIVERSE["etfs"],
                "VOO": {"asset_class": "US", "expense_ratio": 0.0003, "dividend_rate": 0.012, "income_type": "qualified"},
            },
        }
        result = target_allocation_blended_weights(
            target_allocations={"Taxable": {"VTI": 0.5, "VOO": 0.5}},
            value_by_account_type={"Taxable": 10000.0},
            universe=universe,
        )
        assert result["weights_by_asset_class"] == pytest.approx({"US": 1.0})

    def test_account_type_with_value_but_no_target_is_excluded_not_guessed(self):
        result = target_allocation_blended_weights(
            target_allocations={"Taxable": {"VTI": 1.0}},
            value_by_account_type={"Taxable": 50000.0, "Roth IRA": 50000.0},
            universe=TEST_UNIVERSE,
        )
        assert result["weights_by_asset_class"] == pytest.approx({"US": 1.0})
        assert result["covered_value"] == pytest.approx(50000.0)
        assert result["total_value"] == pytest.approx(100000.0)

    def test_nothing_covered_returns_empty_dict(self):
        result = target_allocation_blended_weights(
            target_allocations={},
            value_by_account_type={"Taxable": 50000.0},
            universe=TEST_UNIVERSE,
        )
        assert result["weights_by_asset_class"] == {}
        assert result["covered_value"] == 0.0
        assert result["total_value"] == pytest.approx(50000.0)

    def test_ticker_missing_from_universe_is_skipped_not_a_crash(self):
        result = target_allocation_blended_weights(
            target_allocations={"Taxable": {"VTI": 0.5, "GHOST": 0.5}},
            value_by_account_type={"Taxable": 10000.0},
            universe=TEST_UNIVERSE,
        )
        # GHOST isn't in the universe -- only VTI's own (normalized-against-the-full-1.0-sum,
        # GHOST included) weight contributes, so this isn't silently rescaled up to 100% US.
        assert result["weights_by_asset_class"] == pytest.approx({"US": 0.5})

    def test_weights_sum_to_one_when_covered(self):
        result = target_allocation_blended_weights(
            target_allocations={"Taxable": {"VTI": 0.6}, "Traditional 401(k)": {"BND": 0.4}},
            value_by_account_type={"Taxable": 60000.0, "Traditional 401(k)": 40000.0},
            universe=TEST_UNIVERSE,
        )
        assert sum(result["weights_by_asset_class"].values()) == pytest.approx(1.0)


class TestPortfolioValueByAccountType:
    """WEALTH_BY_ACCOUNT_TYPE_CHARTS.md (2026-08-22) — grouping a flat lot list (the shape
    `modules.investing.create_lots`/`roll_forward_portfolio` produce, and
    `modules.projection.project_multi_year` stores per row as `ending_lots`) by account type, for
    the new "wealth by account type" stacked-area charts."""

    def test_sums_shares_times_price_per_account_type(self):
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 100.0, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "VTI", "account_type": "Taxable", "shares": 50.0, "basis_per_share": 60.0, "year_acquired": 2021},
            {"ticker": "BND", "account_type": "Traditional 401(k)", "shares": 200.0, "basis_per_share": 80.0, "year_acquired": 2022},
        ]
        prices = {"VTI": 100.0, "BND": 80.0}
        result = portfolio_value_by_account_type(lots, prices)
        assert result["Taxable"] == pytest.approx(150.0 * 100.0)
        assert result["Traditional 401(k)"] == pytest.approx(200.0 * 80.0)

    def test_every_account_type_present_even_when_unused(self):
        result = portfolio_value_by_account_type([], {})
        assert set(result.keys()) == set(ACCOUNT_TYPES)
        assert all(v == 0.0 for v in result.values())

    def test_lot_with_unpriced_ticker_is_skipped_not_a_crash(self):
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 100.0, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "GHOST", "account_type": "Taxable", "shares": 999.0, "basis_per_share": 1.0, "year_acquired": 2020},
        ]
        prices = {"VTI": 100.0}  # GHOST has no price entry
        result = portfolio_value_by_account_type(lots, prices)
        assert result["Taxable"] == pytest.approx(100.0 * 100.0)

    def test_sum_of_all_account_types_equals_total_gross_value(self):
        # The exactness bar WEALTH_BY_ACCOUNT_TYPE_CHARTS.md itself calls for: bands must sum to
        # EXACTLY the same total a plain gross-value tally over the same lots/prices would give.
        lots = [
            {"ticker": "VTI", "account_type": "Taxable", "shares": 100.0, "basis_per_share": 50.0, "year_acquired": 2020},
            {"ticker": "BND", "account_type": "Traditional 401(k)", "shares": 200.0, "basis_per_share": 80.0, "year_acquired": 2022},
            {"ticker": "VTI", "account_type": "Roth IRA", "shares": 30.0, "basis_per_share": 90.0, "year_acquired": 2023},
        ]
        prices = {"VTI": 100.0, "BND": 80.0}
        result = portfolio_value_by_account_type(lots, prices)
        expected_total = sum(lot["shares"] * prices[lot["ticker"]] for lot in lots)
        assert sum(result.values()) == pytest.approx(expected_total)
