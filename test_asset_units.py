"""
Asset Units Registry Tests (test_asset_units.py)
=================================================

The registry is the ONLY scale source for deposit amounts and
minimums, so these tests pin:

- the registered scales against their real authorities (wallet for
  USDT, withdrawal_rules.EGP_QUANTUM for EGP) — no invented numbers;
- fail-closed behavior: unknown asset → ``UnknownAssetScaleError``,
  NEVER a default scale;
- exact integer parsing/rendering (no float, no rounding, precision
  bounded by the asset's scale);
- the wallet credit-asset predicate (USDT only) used by the credit
  boundary.

Run:
    python3 -m pytest test_asset_units.py -q
"""

from decimal import Decimal

import pytest

import asset_units
import wallet
import withdrawal_rules


# ── Registry ──────────────────────────────────────────────────────────


class TestRegistry:
    def test_usdt_scale_is_the_wallet_authority(self):
        assert asset_units.decimals_for("USDT") == 8
        assert asset_units.decimals_for("USDT") == wallet.USDT_DECIMALS

    def test_egp_scale_is_the_withdrawal_quantum_exponent(self):
        # EGP_QUANTUM = 0.01 → exactly 2 decimal places.
        assert asset_units.decimals_for("EGP") == 2
        quantum_exponent = -withdrawal_rules.EGP_QUANTUM.as_tuple().exponent
        assert asset_units.decimals_for("EGP") == quantum_exponent

    def test_lookup_is_normalized_without_touching_identity(self):
        assert asset_units.decimals_for("usdt") == 8
        assert asset_units.decimals_for(" USDT ") == 8
        assert asset_units.decimals_for("egp") == 2
        assert asset_units.decimals_for("Egp") == 2
        # Normalization is read-side only: the registered canonical
        # spellings stay exactly as authored.
        assert asset_units.decimals_for("USDT") == 8
        assert asset_units.decimals_for("EGP") == 2

    def test_unknown_asset_fails_closed_with_no_default_scale(self):
        for unknown in ("BTC", "EUR", "usdt2", "", "  ", None, 123, 1.5):
            with pytest.raises(asset_units.UnknownAssetScaleError):
                asset_units.decimals_for(unknown)

    def test_is_supported_mirrors_the_registry(self):
        assert asset_units.is_supported("USDT") is True
        assert asset_units.is_supported("EGP") is True
        assert asset_units.is_supported("BTC") is False
        assert asset_units.is_supported(None) is False
        assert asset_units.is_supported(42) is False

    def test_unknown_asset_raises_type_not_value(self):
        # Callers map by TYPE, never by message text.
        with pytest.raises(asset_units.UnknownAssetScaleError) as excinfo:
            asset_units.decimals_for("BTC")
        assert not isinstance(excinfo.value, asset_units.AssetAmountError)


# ── Exact parsing (atomic units per asset) ────────────────────────────


class TestParseUnits:
    def test_egp_50_is_5000_units_not_usdt_scale(self):
        # THE regression guard: 50 EGP = 5000 (2 dp), NEVER
        # 5_000_000_000 (the old 8-dp USDT interpretation).
        assert asset_units.parse_units("50", "EGP") == 5000
        assert asset_units.parse_units("50", "EGP") != 5_000_000_000

    def test_usdt_1_is_100000000_units(self):
        assert asset_units.parse_units("1", "USDT") == 100_000_000

    def test_egp_precision_is_two_decimals(self):
        assert asset_units.parse_units("49.99", "EGP") == 4999
        assert asset_units.parse_units("0.01", "EGP") == 1
        with pytest.raises(asset_units.AssetAmountError):
            asset_units.parse_units("0.001", "EGP")   # 3 dp → invalid
        with pytest.raises(asset_units.AssetAmountError):
            asset_units.parse_units("50.0001", "EGP")

    def test_usdt_precision_is_eight_decimals(self):
        assert asset_units.parse_units("0.001", "USDT") == 100_000
        assert asset_units.parse_units("1.00000001", "USDT") == 100_000_001
        assert asset_units.parse_units("0.99", "USDT") == 99_000_000
        with pytest.raises(asset_units.AssetAmountError):
            asset_units.parse_units("1.000000001", "USDT")  # 9 dp

    def test_scale_is_not_shared_between_assets(self):
        # The SAME text parses to different units per asset — one
        # global scale is impossible by construction.
        assert asset_units.parse_units("1", "EGP") == 100
        assert asset_units.parse_units("1", "USDT") == 100_000_000

    @pytest.mark.parametrize("bad", [1.5, True, False])
    def test_float_and_bool_are_rejected(self, bad):
        with pytest.raises(asset_units.AssetAmountError):
            asset_units.parse_units(bad, "USDT")

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "-1", "0", "abc", "1.5.2", "1e3", "+5", "1,5", "0x10"],
    )
    def test_malformed_text_is_rejected(self, bad):
        with pytest.raises(asset_units.AssetAmountError):
            asset_units.parse_units(bad, "USDT")

    def test_decimal_and_int_inputs_are_exact(self):
        assert asset_units.parse_units(Decimal("50"), "EGP") == 5000
        assert asset_units.parse_units(50, "EGP") == 5000
        assert asset_units.parse_units(Decimal("1"), "USDT") == 100_000_000

    def test_arabic_indic_digits_normalize(self):
        assert asset_units.parse_units("٥٠", "EGP") == 5000

    def test_overflow_is_rejected(self):
        with pytest.raises(asset_units.AssetAmountError):
            asset_units.parse_units("99999999999999999999", "USDT")

    def test_unknown_asset_never_falls_back_to_a_default_scale(self):
        with pytest.raises(asset_units.UnknownAssetScaleError):
            asset_units.parse_units("50", "BTC")


# ── Exact rendering (display) ─────────────────────────────────────────


class TestRender:
    def test_egp_renders_at_two_decimals(self):
        assert str(asset_units.units_to_asset_decimal(5000, "EGP")) == "50.00"
        assert str(asset_units.units_to_asset_decimal(4999, "EGP")) == "49.99"

    def test_usdt_renders_at_eight_decimals(self):
        assert (
            str(asset_units.units_to_asset_decimal(100_000_000, "USDT"))
            == "1.00000000"
        )

    def test_round_trip_with_parse_is_exact(self):
        for asset, text in (("EGP", "50"), ("USDT", "1.00000001")):
            units = asset_units.parse_units(text, asset)
            rendered = asset_units.units_to_asset_decimal(units, asset)
            assert asset_units.parse_units(str(rendered), asset) == units

    def test_unknown_asset_render_fails_closed(self):
        with pytest.raises(asset_units.UnknownAssetScaleError):
            asset_units.units_to_asset_decimal(5000, "BTC")

    @pytest.mark.parametrize("bad", ["50", 1.5, True, -1, None])
    def test_invalid_units_are_rejected(self, bad):
        with pytest.raises(asset_units.AssetAmountError):
            asset_units.units_to_asset_decimal(bad, "USDT")


# ── Wallet credit currency (credit-boundary gate) ─────────────────────


class TestWalletCreditAsset:
    def test_only_the_wallet_currency_is_creditable(self):
        assert asset_units.is_wallet_credit_asset("USDT") is True
        assert asset_units.is_wallet_credit_asset("usdt") is True
        assert asset_units.is_wallet_credit_asset(" USDT ") is True

    def test_egp_and_unknown_assets_are_not_creditable(self):
        assert asset_units.is_wallet_credit_asset("EGP") is False
        assert asset_units.is_wallet_credit_asset("BTC") is False
        assert asset_units.is_wallet_credit_asset(None) is False
        assert asset_units.is_wallet_credit_asset(42) is False
        assert asset_units.is_wallet_credit_asset("") is False
