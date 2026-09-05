import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

import tools.sector_weekly_scheduler as scheduler
from lib.sector_weekly import (
    CANONICAL_SQLITE_SCHEMA, FULL_REPORT_MAX_CHARS, JST, OUTSIDE_IN_SECTORS, SECTORS,
    SectorValidationError, connect_sector_db, dedupe_key,
    scheduled_sector, sector_name, validate_report, weekly_window,
)
from tools.sector_weekly_scheduler import (
    assemble_payload, run_scheduled,
)
from tools.apply_sector_weekly_work_sqlite_migration import apply_sqlite_migration


def _migrate_fixture(db: Path) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(CANONICAL_SQLITE_SCHEMA)
    conn.close()
    apply_sqlite_migration(
        db, expected_db_path=db, backup_dir=db.parent / "migration_backups",
    )


def _content() -> dict:
    materials = []
    for number in range(1, 4):
        extra = (
            "\n\n**試算**\n\n営業利益影響は10〜20億円。"
            "\n\n**仮説**\n\n需給変化は継続する。"
            if number == 1 else ""
        )
        materials.append(
            f"### 材料{number}：重要ドライバー{number}\n\n"
            "**確認できた事実**\n\n今週の一次資料で需給が変化した。\n\n"
            "**日本企業への波及**\n\n日本企業の数量と単価に波及する。\n\n"
            "**利益への影響**\n\n売上と営業利益への感応度を確認した。\n\n"
            "**株価への織り込み**\n\n会社計画への織り込みは限定的。\n\n"
            "**反対材料・注意点**\n\n価格反落で仮説が崩れる。" + extra
        )
    return {
        "importance": "A+",
        "direction": "mixed",
        "summary_bullets": ["料金改定の影響", "燃料価格の逆風", "原子力稼働率の改善"],
        "watchlist_companies": [{"code": "9503", "name": "関西電力", "direction": "positive"}],
        "next_week_watchpoints": ["燃料価格を確認"],
        "missed_candidates": ["地方自治体資料の更新"],
        "full_report_md": "# 【東証33業種週次】電気・ガス業\n\n## 今週の要旨\n結論の要約。\n\n" + "\n\n\n\n".join(materials),
        "sources": [{
            "title": "電気料金資料", "url": "https://example.com/primary.pdf", "source_name": "資源エネルギー庁",
            "source_type": "government", "published_at": "2026-08-28T10:00:00+09:00",
        }],
    }


def test_fixed_sector_mapping_boundaries():
    assert len(SECTORS) == 33
    assert sector_name(1) == "水産・農林業"
    assert sector_name(20) == "電気・ガス業"
    assert sector_name(33) == "サービス業"


def test_all_33_sector_code_name_pairs_are_canonical():
    expected = (
        "水産・農林業", "鉱業", "建設業", "食料品", "繊維製品", "パルプ・紙",
        "化学", "医薬品", "石油・石炭製品", "ゴム製品", "ガラス・土石製品", "鉄鋼",
        "非鉄金属", "金属製品", "機械", "電気機器", "輸送用機器", "精密機器",
        "その他製品", "電気・ガス業", "陸運業", "海運業", "空運業", "倉庫・運輸関連業",
        "情報・通信業", "卸売業", "小売業", "銀行業", "証券・商品先物取引業", "保険業",
        "その他金融業", "不動産業", "サービス業",
    )
    assert SECTORS == expected
    assert tuple(sector_name(code) for code in range(1, 34)) == expected


@pytest.mark.parametrize(("at", "expected"), [
    ("2026-09-05T06:00:00+09:00", 1),
    ("2026-09-05T23:00:00+09:00", 18),
    ("2026-09-06T00:00:00+09:00", 19),
    ("2026-09-06T14:00:00+09:00", 33),
    ("2026-09-06T15:00:00+09:00", None),
])
def test_schedule_boundaries(at, expected):
    assert scheduled_sector(datetime.fromisoformat(at)) == expected


def test_all_33_sectors_share_the_same_weekly_period():
    expected = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    last = weekly_window(datetime.fromisoformat("2026-09-06T14:00:00+09:00"))
    assert expected == last
    assert expected.period_start.isoformat() == "2026-08-29T06:00:00+09:00"
    assert expected.period_end.isoformat() == "2026-09-05T05:59:59+09:00"


@pytest.mark.parametrize("sector", sorted(OUTSIDE_IN_SECTORS))
def test_global_sectors_receive_mandatory_outside_in_workflow(sector):
    code = SECTORS.index(sector) + 1
    prompt = scheduler.build_prompt(code, weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00")))
    assert "日本企業名や国内ニュースから検索を始めず" in prompt
    assert "世界の価格・需給・在庫・設備投資・政策・規制" in prompt
    assert "日本企業名が書かれていない海外材料" in prompt
    assert prompt.index("世界の価格・需給") < prompt.index("日本上場企業のどの事業へ波及")


@pytest.mark.parametrize("sector", ["鉱業", "非鉄金属"])
def test_mining_and_nonferrous_prompts_screen_minor_metals(sector):
    prompt = scheduler.build_prompt(SECTORS.index(sector) + 1, weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00")))
    for term in ("アンチモン", "タングステン", "ガリウム", "ゲルマニウム", "タンタル", "ニオブ", "TC/RC"):
        assert term in prompt
    assert "閾値は機械的な除外条件ではなく" in prompt
    assert "5群を別々の検索query" in prompt
    assert "5群の検索証跡がない状態で調査完了としてはいけません" in prompt
    assert "BHP・Rio Tinto・Vale・Glencore・Freeport-McMoRan" in prompt
    assert "確認した一次資料をsourcesへ含めてください" in prompt


@pytest.mark.parametrize("sector", ["機械", "電気機器", "精密機器"])
def test_semiconductor_related_prompts_require_global_peers_and_capex_mapping(sector):
    prompt = scheduler.build_prompt(SECTORS.index(sector) + 1, weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00")))
    for term in ("Samsung Electronics", "SK hynix", "Micron", "3社すべて", "個別に検索", "TSMC", "ASML", "Applied Materials", "ASE", "NVIDIA", "CAPEX", "受注までの時間差"):
        assert term in prompt
    assert "粗い結論は禁止" in prompt


def test_shipping_prompt_separates_freight_markets():
    prompt = scheduler.build_prompt(22, weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00")))
    for term in ("VLCC", "Product tanker", "Dry bulk", "Container", "LNG船", "LPG船", "自動車船"):
        assert term in prompt
    assert "一つの運賃指数だけ" in prompt


def test_final_prompt_requires_concise_report_and_bullets():
    prompt = scheduler.build_prompt(16, weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00")))
    for term in ("重要材料は原則3〜5件", "hard上限は10,000文字", "5,500文字を超えたことだけでは失敗とせず", "機械的な途中切断", "summary_bulletsは左カード用に3〜5件", "同じ数値・因果関係", "URLとMarkdown citationはfull_report_mdへ入れず"):
        assert term in prompt


def test_final_prompt_requires_japanese_reader_labels_notes_and_readable_numbers():
    prompt = scheduler.build_prompt(
        13, weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00")),
    )
    for term in (
        "**確認できた事実**", "**日本企業への波及**", "**利益への影響**",
        "**株価への織り込み**", "**反対材料・注意点**", "**試算**", "**仮説**",
        "ラベルの次に空行を1行", "空行を3行以上", "※Glencore（グレンコア）",
        "日本語読み", "1段落1論点", "3件以上", "箇条書き", "数字だけを羅列",
    ):
        assert term in prompt


def test_validate_preserves_arrays_markdown_sources_and_stable_key():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    payload = assemble_payload(_content(), 20, window, datetime.fromisoformat("2026-09-06T01:02:33+09:00"))
    validated = validate_report(payload, expected_code=20, expected_window=window).report
    assert json.loads(validated["summary_bullets"])[0] == "料金改定の影響"
    assert json.loads(validated["sources"])[0]["source_type"] == "government"
    assert validated["full_report_md"].startswith("# 【東証33業種週次】")
    assert validated["dedupe_key"] == "sector_weekly:2026-09-05:20"


@pytest.mark.parametrize("label", ["確認できた事実", "日本企業への波及", "利益への影響", "株価への織り込み", "反対材料・注意点"])
def test_material_structure_requires_each_exact_core_label_per_material(label):
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    payload = assemble_payload(content, 20, window)
    validate_report(
        payload, expected_code=20, expected_window=window, require_new_markdown_style=True,
    )
    content["full_report_md"] = content["full_report_md"].replace(f"**{label}**", label, 1)
    with pytest.raises(SectorValidationError, match=rf"material 1.*{re.escape(label)}"):
        validate_report(
            assemble_payload(content, 20, window), expected_code=20, expected_window=window,
            require_new_markdown_style=True,
        )


@pytest.mark.parametrize("label", ["試算", "仮説"])
def test_labels_outside_materials_do_not_satisfy_structure(label):
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    content["full_report_md"] = content["full_report_md"].replace(f"**{label}**", label, 1)
    content["full_report_md"] += f"\n\n## Sources\n\n**{label}**\n\nここは対象外。"
    with pytest.raises(SectorValidationError, match=rf"standalone \*\*{label}\*\*"):
        validate_report(
            assemble_payload(content, 20, window), expected_code=20, expected_window=window,
            require_new_markdown_style=True,
        )


def test_new_stage_requires_standalone_label_paragraphs():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    validate_report(
        assemble_payload(content, 20, window), expected_code=20, expected_window=window,
        require_new_markdown_style=True,
    )
    inline = _content()
    inline["full_report_md"] = inline["full_report_md"].replace(
        "**確認できた事実**\n\n今週の一次資料で需給が変化した。",
        "**確認できた事実**：今週の一次資料で需給が変化した。",
        1,
    )
    with pytest.raises(SectorValidationError, match="standalone Japanese label paragraphs"):
        validate_report(
            assemble_payload(inline, 20, window), expected_code=20, expected_window=window,
            require_new_markdown_style=True,
        )


@pytest.mark.parametrize("separator", ["\n\n", "\n\n\n", "\n\n\n\n", "\n\n\n\n\n"])
def test_new_stage_does_not_reject_material_headings_for_blank_line_count(separator):
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    content["full_report_md"] = content["full_report_md"].replace(
        "\n\n\n\n### 材料2", f"{separator}### 材料2", 1,
    ).replace(
        "\n\n\n\n### 材料3", f"{separator}### 材料3", 1,
    )
    validate_report(
        assemble_payload(content, 20, window), expected_code=20, expected_window=window,
        require_new_markdown_style=True,
    )


def test_new_stage_still_requires_material_heading_on_its_own_line():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    content["full_report_md"] = content["full_report_md"].replace(
        "\n\n\n\n### 材料2", "### 材料2", 1,
    )
    with pytest.raises(SectorValidationError, match="3..5 sections"):
        validate_report(
            assemble_payload(content, 20, window), expected_code=20, expected_window=window,
            require_new_markdown_style=True,
        )


def test_recovery_accepts_legacy_english_labels_but_new_stage_rejects_them():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    replacements = {
        "**確認できた事実**\n\n": "**Fact**: ",
        "**日本企業への波及**\n\n": "**Transmission**: ",
        "**利益への影響**\n\n": "**Magnitude**: ",
        "**株価への織り込み**\n\n": "**Pricing-in**: ",
        "**反対材料・注意点**\n\n": "**Counterevidence**: ",
        "**試算**\n\n": "**Estimate**: ",
        "**仮説**\n\n": "**Hypothesis**: ",
    }
    for current, legacy in replacements.items():
        content["full_report_md"] = content["full_report_md"].replace(current, legacy)
    payload = assemble_payload(content, 20, window)
    validate_report(payload, expected_code=20, expected_window=window)
    with pytest.raises(SectorValidationError, match="standalone Japanese label paragraphs"):
        validate_report(
            payload, expected_code=20, expected_window=window, require_new_markdown_style=True,
        )


def test_new_stage_rejects_wrong_material_section_count():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    two = _content()
    two["full_report_md"] = two["full_report_md"].split("### 材料3：", 1)[0]
    with pytest.raises(SectorValidationError, match="3..5 sections"):
        validate_report(
            assemble_payload(two, 20, window), expected_code=20, expected_window=window,
            require_new_markdown_style=True,
        )
    six = _content()
    third = "### 材料3：" + six["full_report_md"].split("### 材料3：", 1)[1]
    six["full_report_md"] += "\n\n\n\n" + third.replace("材料3：", "材料4：", 1)
    six["full_report_md"] += "\n\n\n\n" + third.replace("材料3：", "材料5：", 1)
    six["full_report_md"] += "\n\n\n\n" + third.replace("材料3：", "材料6：", 1)
    with pytest.raises(SectorValidationError, match="3..5 sections"):
        validate_report(
            assemble_payload(six, 20, window), expected_code=20, expected_window=window,
            require_new_markdown_style=True,
        )


def test_wrong_period_and_invalid_json_shape_are_rejected():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    payload = assemble_payload(_content(), 20, window)
    payload["period_start"] = "2026-08-30T06:00:00+09:00"
    with pytest.raises(SectorValidationError, match="common weekly window"):
        validate_report(payload, expected_code=20, expected_window=window)
    payload = assemble_payload(_content(), 20, window)
    payload["summary_bullets"] = [f"bullet-{index}" for index in range(6)]
    with pytest.raises(SectorValidationError, match="3..5"):
        validate_report(payload, expected_code=20, expected_window=window)


def test_source_date_precision_is_preserved_without_inventing_time():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    content["sources"][0]["published_at"] = "2026-08-28"
    payload = assemble_payload(content, 20, window)
    source = json.loads(validate_report(payload, expected_code=20, expected_window=window).report["sources"])[0]
    assert source["published_at"] == "2026-08-28"


def test_card_bullets_remove_web_citations_and_are_bounded():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    content["summary_bullets"][0] = "重要材料 " + "長" * 300 + " ([example.com](https://example.com/source))"
    payload = assemble_payload(content, 20, window)
    assert len(payload["summary_bullets"][0]) <= 240
    assert "https://" not in payload["summary_bullets"][0]
    payload = assemble_payload(_content(), 20, window)
    payload["summary_bullets"] = "not-an-array"
    with pytest.raises(SectorValidationError, match="summary_bullets"):
        validate_report(payload, expected_code=20, expected_window=window)


def test_full_report_normalizes_title_and_citations():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    content["importance"] = "A"
    content["full_report_md"] = "# 電気・ガス業\n\n本文 ([一次資料](https://example.com/source))"
    payload = assemble_payload(content, 20, window)
    assert payload["full_report_md"].startswith("# 【東証33業種週次】電気・ガス業")
    assert "https://" not in payload["full_report_md"]


@pytest.mark.parametrize("length", [5_500, 5_501, 6_864, 9_999, 10_000])
def test_full_report_hard_limit_accepts_every_required_boundary(length: int):
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    content["importance"] = "A"
    body = content["full_report_md"]
    content["full_report_md"] = body + "長" * (length - len(body))

    payload = assemble_payload(content, 20, window)
    validated = validate_report(
        payload, expected_code=20, expected_window=window, require_new_markdown_style=True,
    )

    assert FULL_REPORT_MAX_CHARS == 10_000
    assert len(payload["full_report_md"]) == length
    assert len(validated.report["full_report_md"]) == length


def test_full_report_hard_limit_rejects_10001_without_truncating():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    body = content["full_report_md"]
    content["full_report_md"] = body + "長" * (10_001 - len(body))

    with pytest.raises(SectorValidationError, match="10,000-character hard limit"):
        assemble_payload(content, 20, window)
    assert len(content["full_report_md"]) == 10_001


def test_full_report_restores_concatenated_markdown_headings():
    window = weekly_window(datetime.fromisoformat("2026-09-05T06:00:00+09:00"))
    content = _content()
    content["full_report_md"] = "# 電気・ガス業\n結論です。 ## 今週の要旨\n要旨です。 ### 1. 重要材料\nFactです。"
    payload = assemble_payload(content, 20, window)
    assert "結論です。\n\n## 今週の要旨" in payload["full_report_md"]
    assert "要旨です。\n\n### 1. 重要材料" in payload["full_report_md"]


def test_scheduler_enqueues_idempotently_without_research(tmp_path: Path, monkeypatch):
    db = tmp_path / "news.db"
    _migrate_fixture(db)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    at = datetime.fromisoformat("2026-09-05T06:00:00+09:00")
    first = run_scheduled(at, db_path=db, log_path=tmp_path / "log.jsonl", lock_path=tmp_path / "lock")
    second = run_scheduled(at, db_path=db, log_path=tmp_path / "log.jsonl", lock_path=tmp_path / "lock")
    assert first["status"] == "queued"
    assert first["assignment_status"] == "ready"
    assert first["created"] is True
    assert second["status"] == "slot_already_processed"
    assert second["created"] is False
    assert first["assignment_id"] == second["assignment_id"]
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM sector_weekly_work_assignments").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM canonical_sector_reports").fetchone()[0] == 0


def test_sector_weekly_production_reachable_modules_have_no_openai_api_calls():
    root = Path(__file__).parents[1]
    sources = "\n".join(
        (root / relative).read_text(encoding="utf-8")
        for relative in (
            "tools/sector_weekly_scheduler.py",
            "tools/sector_weekly_work_bridge.py",
            "tools/sector_weekly_inbox_worker.py",
            "lib/sector_weekly_work.py",
        )
    )
    for forbidden in ("OPENAI_API_KEY", "OpenAI(", "responses.create", "client.responses", "max_tool_calls"):
        assert forbidden not in sources


def test_chatgpt_worker_prompt_has_transport_research_and_schema_contracts():
    prompt = (Path(__file__).parents[1] / "config" / "sector_weekly_worker_prompt.txt").read_text(encoding="utf-8")
    for term in (
        "Sector Weekly Worker", "1回の実行でready assignmentを最大1件", "claim --owner sector-weekly-worker",
        "ChatGPTプラン内", "outside-in", "確認できた事実", "日本企業への波及", "反対材料・注意点",
        "海外企業", "日本語読み", "空行を3行以上", "数字は1段落1論点",
        "重要材料は", "3〜5件", "hard上限は10,000文字", "missed_candidates",
        "sector_weekly_work_result_v2", "company_ir | government | regulator",
        "heartbeat --assignment-id", "10分ごと", "hard time budgetは50分", "45分時点",
        "abandon --assignment-id", "atomicにretry_pending", "調査品質を落として無理にstage",
        "validate-and-stage --assignment-id", "handoff_pending", "sync_pending", "SectorWeeklyInboxWorker",
        "built-in Web Search", "Responses API", "OPENAI_API_KEY",
        "1実行で複数sectorを処理してはいけません", "月曜08:05 JST",
        "不変contract", "verify-claim", "validate-and-stage", "contract hash", "attempt固有",
        "codeからnameを推測", "memoryは調査入力に使わない", "隔離SQLite DB",
    ):
        assert term in prompt
    assert prompt.count("sector_weekly_work_bridge.py verify-claim") == 1
    assert "verify-payload" not in prompt
    for forbidden in (
        "API料金", "max_tool_calls", "API timeout", "token料金",
        '"sector_code": 5', '"sector_name": "鉱業"',
    ):
        assert forbidden not in prompt


def test_company_news_schema_is_not_modified_by_sector_migration():
    root = Path(__file__).parents[1]
    for migration in ("017_sector_weekly_reports.sql", "018_sector_weekly_work_assignments.sql"):
        sql = (root / "migrations" / migration).read_text(encoding="utf-8")
        assert "ALTER TABLE canonical_news_events" not in sql
        assert "company_news_work" not in sql
    assert "api_latest_news_stream" in (root / "migrations" / "017_sector_weekly_reports.sql").read_text(encoding="utf-8")


def test_sector_weekly_task_installer_uses_hourly_disabled_safe_schedule():
    script = (Path(__file__).parents[1] / "tools" / "install_sector_weekly_task.ps1").read_text(encoding="utf-8")
    assert "-At $firstSaturday -RepetitionInterval (New-TimeSpan -Hours 1)" in script
    assert 'Repetition = "PT1H"' in script
    assert 'RepetitionDuration = "Indefinite"' in script
    assert "EligibleSlots = 51" in script
    assert "EligibleWindowEnd = $firstSaturday.AddHours(50)" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "if (-not $Enable) { Disable-ScheduledTask" in script
    assert "New-TimeSpan -Minutes 45" not in script
    assert 'Repetition = "PT45M"' not in script


def test_scheduler_can_be_enabled_without_running_before_first_saturday(tmp_path: Path):
    result = run_scheduled(
        datetime.fromisoformat("2026-08-30T10:00:00+09:00"),
        db_path=tmp_path / "news.db", log_path=tmp_path / "log.jsonl", lock_path=tmp_path / "lock",
        not_before=datetime.fromisoformat("2026-09-05T06:00:00+09:00"),
    )
    assert result["status"] == "not_started"


def test_stale_scheduler_lock_is_recovered(tmp_path: Path, monkeypatch):
    lock = tmp_path / "scheduler.lock"
    lock.write_text("pid=42424242\n", encoding="utf-8")
    monkeypatch.setattr(scheduler, "_pid_is_alive", lambda _pid: False)
    result = run_scheduled(
        datetime.fromisoformat("2026-08-30T10:00:00+09:00"), db_path=tmp_path / "news.db",
        log_path=tmp_path / "log.jsonl", lock_path=lock,
        not_before=datetime.fromisoformat("2026-09-05T06:00:00+09:00"),
    )
    assert result["status"] == "not_started"
    assert not lock.exists()
