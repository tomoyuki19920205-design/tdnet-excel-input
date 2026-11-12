import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lib.ny_market_research import archive_research_response


def test_same_url_concurrent_responses_keep_each_original(tmp_path):
    common = dict(
        url="https://issuer.example/news",
        ticker="NXXT",
        issuer_name="NextNRG, Inc.",
        report_date_jst="2026-11-12",
        output_dir=tmp_path,
        http_status=200,
    )
    bodies = [f"response version {i}".encode() for i in range(24)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(lambda body: archive_research_response(body, **common), bodies))

    assert len({record["path"] for record in records}) == len(bodies)
    for body, record in zip(bodies, records):
        assert record["sha256"] == hashlib.sha256(body).hexdigest()
        assert Path(record["path"]).read_bytes() == body
        assert record["issuer_identity_status"] == "unverified"


def test_identical_response_can_be_reused_without_changing_bytes(tmp_path):
    common = dict(
        url="https://issuer.example/news",
        ticker="NXXT",
        issuer_name="NextNRG, Inc.",
        report_date_jst="2026-11-12",
        output_dir=tmp_path,
        http_status=403,
    )
    first = archive_research_response(b"denied", **common)
    second = archive_research_response(b"denied", **common)
    assert first["path"] == second["path"]
    assert len(list(tmp_path.glob("*.raw"))) == 1
    assert Path(first["path"]).read_bytes() == b"denied"
