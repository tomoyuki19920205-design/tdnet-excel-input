from src.events.earnings_production_pipeline import _resolve_exact_fiscal_period


def test_exact_xbrl_period_overrides_title_month_end(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda _path: True)
    monkeypatch.setattr(
        "src.segment.zip_identity_verifier.extract_actual_metadata_from_zip",
        lambda _path, expected_quarter="": {
            "ticker": "9999",
            "period": "2027-03-20",
            "quarter": expected_quarter,
        },
    )

    assert _resolve_exact_fiscal_period(
        ticker="9999",
        quarter="1Q",
        title="2027年3月期 第1四半期決算短信",
        xbrl_path="exact.zip",
    ) == "2027-03-20"


def test_mismatched_xbrl_identity_falls_back_to_title_hint(monkeypatch):
    monkeypatch.setattr("os.path.exists", lambda _path: True)
    monkeypatch.setattr(
        "src.segment.zip_identity_verifier.extract_actual_metadata_from_zip",
        lambda _path, expected_quarter="": {
            "ticker": "8888",
            "period": "2027-03-20",
            "quarter": expected_quarter,
        },
    )

    assert _resolve_exact_fiscal_period(
        ticker="9999",
        quarter="1Q",
        title="2027年3月期 第1四半期決算短信",
        xbrl_path="wrong-company.zip",
    ) == "2027-03-31"
