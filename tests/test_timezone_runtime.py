from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, reset_tzpath, TZPATH
import pytest

@pytest.mark.parametrize('packaged', [False, True])
def test_tokyo_new_york_dst(packaged):
    original = TZPATH
    try:
        if packaged:
            reset_tzpath(())
        ZoneInfo.clear_cache()
        tokyo = ZoneInfo('Asia/Tokyo')
        ny = ZoneInfo('America/New_York')
        assert datetime(2025, 3, 9, 12, tzinfo=tokyo).utcoffset() == timedelta(hours=9)
        assert datetime(2025, 3, 9, 1, 59, tzinfo=ny).utcoffset() == timedelta(hours=-5)
        assert datetime(2025, 3, 9, 3, 0, tzinfo=ny).utcoffset() == timedelta(hours=-4)
        assert datetime(2025, 11, 2, 1, 30, tzinfo=ny, fold=0).utcoffset() == timedelta(hours=-4)
        assert datetime(2025, 11, 2, 1, 30, tzinfo=ny, fold=1).utcoffset() == timedelta(hours=-5)
    finally:
        reset_tzpath(original)
        ZoneInfo.clear_cache()
