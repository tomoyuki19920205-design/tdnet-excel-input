"""Offline dependency and auxiliary-state checks; no external connection."""
from pathlib import Path
from datetime import date


def test_postgres_driver_constructs_without_connecting(monkeypatch):
    import psycopg2
    from psycopg2.extensions import make_dsn, parse_dsn
    from psycopg2 import sql
    def forbidden(*args, **kwargs):
        raise AssertionError('No real PostgreSQL connection allowed')
    monkeypatch.setattr(psycopg2, 'connect', forbidden)
    dsn = make_dsn(dbname='fixture', host='localhost', connect_timeout=1)
    assert parse_dsn(dsn)['dbname'] == 'fixture'
    assert isinstance(sql.SQL('select {}').format(sql.Identifier('fixture')), sql.Composed)


def test_dateutil_relativedelta_and_parser():
    from dateutil.relativedelta import relativedelta
    from dateutil.parser import parse
    assert date(2024, 1, 31) + relativedelta(months=1) == date(2024, 2, 29)
    assert parse('2024-10-31').date() == date(2024, 10, 31)


def test_migration_named_fixture_explicit_path(tmp_path):
    from src.migration.migration_db import MigrationDB
    # This name is an explicit caller choice, not an implicit Production DB.
    path = tmp_path/'old state'/'migration.db'
    old = MigrationDB(str(path))
    old.upsert_quarterly_result('1234','2024-10-31','3Q',sales=100,operating_profit=10)
    old.close()
    new = MigrationDB(str(path))
    assert new.get_quarterly_result('1234','2024-10-31','3Q')['sales'] == 100
    new.upsert_quarterly_result('1234','2024-10-31','3Q',sales=101)
    new.close()
    old_again = MigrationDB(str(path))
    assert old_again.get_quarterly_result('1234','2024-10-31','3Q')['sales'] == 101
    old_again.close()
