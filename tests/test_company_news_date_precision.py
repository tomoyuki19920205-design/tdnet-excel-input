"""Offline-only regression tests for the proposed Company News date contract."""
import pytest

from tools import company_news_date_precision as precision


def test_empty_document_date_becomes_unknown_without_arbitrary_date():
    projected = precision.project_document_date("", "2026-08-25", "2026-08-31")
    assert projected == {"date": None, "date_precision": "unknown", "window_status": "unknown",
                         "eligible_as_window_candidate": False}


def test_valid_document_date_is_preserved_and_window_checked():
    projected = precision.project_document_date("2026-08-31", "2026-08-25", "2026-08-31")
    assert projected["date"] == "2026-08-31"
    assert projected["window_status"] == "in_window"
    assert projected["eligible_as_window_candidate"] is True


def test_out_of_window_document_is_not_a_window_candidate():
    projected = precision.project_document_date("2026-05-13", "2026-08-25", "2026-08-31")
    assert projected["window_status"] == "outside_window"
    assert projected["eligible_as_window_candidate"] is False


def test_exact_timezone_datetime_is_preserved():
    value = precision.classify_publication("2026-09-05T09:30:00+09:00")
    assert value == {"published_at": "2026-09-05T09:30:00+09:00",
                     "published_date": "2026-09-05", "published_precision": "datetime"}


def test_date_only_evidence_does_not_create_time_or_timezone():
    value = precision.classify_publication("2026-08-31")
    assert value == {"published_at": None, "published_date": "2026-08-31",
                     "published_precision": "date"}
    assert "T" not in value["published_date"]


def test_unknown_publication_does_not_create_timestamp():
    assert precision.classify_publication(None) == {
        "published_at": None, "published_date": None, "published_precision": "unknown"}


@pytest.mark.parametrize("unrelated", ["2026-09-05T12:00:00+09:00", "2026-09-05T12:01:00+09:00"])
def test_observation_timestamps_are_never_used_as_publication(unrelated):
    # checked_at and first_seen_at are intentionally not inputs to the classifier.
    value = precision.classify_publication(None)
    assert value["published_at"] is None and unrelated not in value.values()


def test_date_only_sort_and_dedupe_are_explicit_and_stable():
    date_only = precision.classify_publication("2026-08-31")
    exact = precision.classify_publication("2026-08-31T09:30:00+09:00")
    unknown = precision.classify_publication(None)
    assert precision.publication_sort_key(exact, "b") > precision.publication_sort_key(date_only, "a")
    assert precision.publication_sort_key(date_only, "a") > precision.publication_sort_key(unknown, "z")
    assert precision.publication_dedupe_component(date_only) == "date:2026-08-31"
    assert precision.publication_dedupe_component(exact).startswith("datetime:2026-08-31T09:30:00")


def test_existing_exact_datetime_behavior_is_unchanged():
    raw = "2026-08-31T09:30:00+09:00"
    value = precision.validate_publication_record(precision.classify_publication(raw))
    assert value["published_at"] == raw


def test_exact_datetime_may_omit_calendar_date_without_conversion():
    value = precision.validate_publication_record({
        "published_at": "2026-08-31T23:30:00-04:00", "published_date": None,
        "published_precision": "datetime"})
    assert value["published_at"] == "2026-08-31T23:30:00-04:00"
    assert value["published_date"] is None
    assert precision.publication_sort_key(value, "legacy")[0] == "2026-08-31"


def test_invalid_or_naive_publication_values_fail_closed():
    with pytest.raises(precision.DatePrecisionError):
        precision.classify_publication("2026-08-31T09:30:00")
    with pytest.raises(precision.DatePrecisionError):
        precision.normalize_document_date("not-a-date")
