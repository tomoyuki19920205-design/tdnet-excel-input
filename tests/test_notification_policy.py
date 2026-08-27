from unittest.mock import patch

import pytest

from src.events.common_models import EventRecord
from src.events.notification_policy import (
    is_correction_disclosure_title,
    normalize_notification_title,
)
from src.events.tdnet_event_store import save_event_to_supabase


@pytest.mark.parametrize(
    "title",
    [
        '(訂正)「2026年6月期決算短信」の一部訂正について',
        '（訂正）「2026年6月期決算短信」の一部訂正について',
        '(訂正・数値データ訂正)「2026年6月期決算短信」の一部訂正について',
        '（訂正・数値データ訂正）「2026年6月期決算短信」の一部訂正について',
        '訂正・数値データ訂正 決算短信',
        '決算短信の一部訂正について',
        '決算短信の再訂正について',
        '決算短信 訂正のお知らせ',
        '決算短信 数値データ訂正',
        '決算短信の一部変更について',
        '  （ 訂正 ）  決算短信  ',
    ],
)
def test_correction_disclosures_are_not_notification_eligible(title: str) -> None:
    assert is_correction_disclosure_title(title)


@pytest.mark.parametrize(
    "title",
    [
        '2026年6月期決算短信〔日本基準〕(連結)',
        '通期業績予想の修正に関するお知らせ',
        '配当予想の修正に関するお知らせ',
        '決算期変更のお知らせ',
    ],
)
def test_normal_important_disclosures_remain_eligible(title: str) -> None:
    assert not is_correction_disclosure_title(title)


def test_normalization_uses_nfkc_and_collapses_whitespace() -> None:
    assert normalize_notification_title('  （訂正）\u3000 決算短信  ') == '(訂正) 決算短信'


def test_store_boundary_suppresses_correction_without_touching_supabase() -> None:
    event = EventRecord(
        ticker='3538',
        company_name='ウイルプラスＨＤ',
        disclosure_datetime='2026-08-27T18:00:00+09:00',
        title='(訂正・数値データ訂正)「2026年６月期決算短信〔日本基準〕(連結)」の一部訂正について',
        doc_url='https://www.release.tdnet.info/inbs/140120260827527408.pdf',
        event_type='earnings',
    )

    with patch('src.events.tdnet_event_store._get_supabase') as get_supabase:
        result = save_event_to_supabase(event)

    get_supabase.assert_not_called()
    assert result == {
        'action': 'dedup_skipped',
        'reason': 'correction_disclosure',
        'notification_suppressed': True,
        'dedupe_key': '',
    }
