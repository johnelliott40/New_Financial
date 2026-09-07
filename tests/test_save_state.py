from modules.save_state import delete_state, list_saves, load_state, save_state

MACRO = {"inflation_rate": 0.025, "pretax_tax_rate": 0.22, "capital_gains_rate": 0.15}
DEMOGRAPHICS = {
    "birth_date": "1990-01-15",
    "retirement_date": "2050-01-01",
    "health_status": "Good",
    "saving_stop_age": 65,
}
ACCOUNTS = [{"name": "Brokerage", "type": "Taxable"}]
POSITIONS = {"Brokerage": [{"ticker": "VTI", "shares": 10.0, "cost_basis_per_share": 200.0}]}
MANUAL_QUOTES = {}
TICKER_UNIVERSE = {"VTI": "US"}
ASSET_CLASS_RETURNS = {"US": 0.065}
TAX = {"filing_status": "single", "year_select": 2026, "w2_gross": 80000.0}
PROJECTION = {
    "filing_status": "single",
    "already_earned_w2": 40000.0,
    "income_breaks": [
        {
            "id": "b1",
            "label": "Sabbatical",
            "start_date": "2026-10-01",
            "end_date": "2027-01-01",
            "annualized_income_during_break": 12000.0,
        }
    ],
}
TARGET_ALLOCATIONS = {"Taxable": {"VTI": 0.6, "BND": 0.4}, "Roth IRA": {"VTI": 1.0}}
CUSTOM_ASSET_CLASSES = {"DMSCV": {"label": "Developed Markets Small-Cap Value", "nominal_return": 0.08}}
REMOVED_CANONICAL_ASSET_CLASSES = ["EM", "TOTV"]
SOCIAL_SECURITY = {"historical_ss_earnings": {"2020": 60000.0, "2021": 62000.0}}


def _save(display_name, saves_dir, **overrides):
    kwargs = dict(
        macro=MACRO,
        demographics=DEMOGRAPHICS,
        accounts=ACCOUNTS,
        positions=POSITIONS,
        manual_quotes=MANUAL_QUOTES,
        ticker_universe=TICKER_UNIVERSE,
        asset_class_returns=ASSET_CLASS_RETURNS,
        tax=TAX,
        projection=PROJECTION,
        target_allocations=TARGET_ALLOCATIONS,
        custom_asset_classes=CUSTOM_ASSET_CLASSES,
        removed_canonical_asset_classes=REMOVED_CANONICAL_ASSET_CLASSES,
        social_security=SOCIAL_SECURITY,
        saves_dir=saves_dir,
    )
    kwargs.update(overrides)
    return save_state(display_name, **kwargs)


def test_list_saves_empty_when_dir_missing(tmp_path):
    assert list_saves(saves_dir=tmp_path / "does_not_exist") == []


def test_save_then_load_round_trips(tmp_path):
    filename = _save("My plan", tmp_path)
    loaded = load_state(filename, saves_dir=tmp_path)

    assert loaded["display_name"] == "My plan"
    assert loaded["macro"] == MACRO
    assert loaded["demographics"] == DEMOGRAPHICS
    assert loaded["accounts"] == ACCOUNTS
    assert loaded["positions"] == POSITIONS
    assert loaded["ticker_universe"] == TICKER_UNIVERSE
    assert loaded["asset_class_returns"] == ASSET_CLASS_RETURNS
    assert loaded["tax"] == TAX
    assert loaded["projection"] == PROJECTION
    assert loaded["target_allocations"] == TARGET_ALLOCATIONS
    assert loaded["custom_asset_classes"] == CUSTOM_ASSET_CLASSES
    assert loaded["removed_canonical_asset_classes"] == REMOVED_CANONICAL_ASSET_CLASSES
    assert loaded["social_security"] == SOCIAL_SECURITY
    assert "saved_at" in loaded


def test_save_without_tax_or_projection_defaults_to_empty_dicts(tmp_path):
    # tax/projection/target_allocations/custom_asset_classes/removed_canonical_asset_classes/
    # social_security are optional kwargs (default None) so every pre-existing call site -- and
    # every save written before each feature existed -- stays valid.
    filename = _save(
        "No tax data",
        tmp_path,
        tax=None,
        projection=None,
        target_allocations=None,
        custom_asset_classes=None,
        removed_canonical_asset_classes=None,
        social_security=None,
    )
    loaded = load_state(filename, saves_dir=tmp_path)
    assert loaded["tax"] == {}
    assert loaded["projection"] == {}
    assert loaded["target_allocations"] == {}
    assert loaded["custom_asset_classes"] == {}
    assert loaded["removed_canonical_asset_classes"] == []
    assert loaded["social_security"] == {}


def test_save_slugifies_the_filename(tmp_path):
    filename = _save("My Plan #1!", tmp_path)
    assert filename == "my_plan_1.json"


def test_save_with_same_name_overwrites(tmp_path):
    _save("Plan A", tmp_path)
    updated_macro = {**MACRO, "inflation_rate": 0.03}
    filename = _save("Plan A", tmp_path, macro=updated_macro)

    assert len(list_saves(saves_dir=tmp_path)) == 1
    assert load_state(filename, saves_dir=tmp_path)["macro"]["inflation_rate"] == 0.03


def test_list_saves_returns_newest_first(tmp_path):
    _save("Older", tmp_path)
    # Force a distinct, later saved_at than "Older" by writing it directly.
    filename_older = list_saves(saves_dir=tmp_path)[0]["filename"]
    older_data = load_state(filename_older, saves_dir=tmp_path)
    older_data["saved_at"] = "2020-01-01T00:00:00"
    (tmp_path / filename_older).write_text(__import__("json").dumps(older_data))

    _save("Newer", tmp_path)

    saves = list_saves(saves_dir=tmp_path)
    assert saves[0]["display_name"] == "Newer"


def test_delete_state_removes_the_file(tmp_path):
    filename = _save("Temp", tmp_path)
    assert len(list_saves(saves_dir=tmp_path)) == 1

    delete_state(filename, saves_dir=tmp_path)

    assert list_saves(saves_dir=tmp_path) == []


def test_delete_state_missing_file_is_a_no_op(tmp_path):
    delete_state("does_not_exist.json", saves_dir=tmp_path)  # should not raise


def test_load_state_of_pre_module_2_save_missing_new_keys(tmp_path):
    """
    A save written before this session's schema change won't have demographics/ticker_universe/
    asset_class_returns keys at all. load_state must return it as-is (no KeyError) — it's the
    caller's job (app.py) to .get() sensible defaults for missing keys, the same pattern the
    (now-removed, 2026-08-30) capital_gains_rate field used to need for its own backward compat.
    """
    import json

    tmp_path.mkdir(parents=True, exist_ok=True)
    old_style = {
        "display_name": "Old save",
        "saved_at": "2026-08-01T00:00:00",
        "macro": {"inflation_rate": 0.025, "age": 40, "retirement_date": "2050-01-01", "pretax_tax_rate": 0.22},
        "accounts": ACCOUNTS,
        "positions": POSITIONS,
        "manual_quotes": MANUAL_QUOTES,
    }
    (tmp_path / "old_save.json").write_text(json.dumps(old_style))

    loaded = load_state("old_save.json", saves_dir=tmp_path)
    assert loaded["display_name"] == "Old save"
    assert "ticker_universe" not in loaded
    assert loaded["macro"]["age"] == 40
