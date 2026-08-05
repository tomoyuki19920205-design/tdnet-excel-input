from tools.audit_legacy_alpha_mapping import classify, load_legacy_mapping


def test_all_legacy_mapping_entries_are_restored():
    mapping = load_legacy_mapping(__import__("pathlib").Path(__file__).resolve().parent.parent)
    assert len(mapping) == 145
    assert mapping["41800"] == "418A"
    assert mapping["47200"] == "472A"


def test_value_match_alone_is_not_confirmed_contamination():
    assert classify(3, 1) == "SUSPECTED_REQUIRES_LINEAGE"


def test_no_rows_is_not_applicable():
    assert classify(0, 0) == "NOT_APPLICABLE"
