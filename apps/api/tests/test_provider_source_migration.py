from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import CheckViolation, RaiseException, UniqueViolation


ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "supabase" / "migrations"
COMPATIBILITY = ROOT / "docker" / "postgres" / "000-supabase-compat.sql"
TARGET = MIGRATIONS / "20260731110000_provider_source_schema.sql"
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


def _database_url(base_url: str, database: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"/{database}",
            parsed.query,
            parsed.fragment,
        )
    )


def _create_database(base_url: str) -> tuple[str, str]:
    name = f"setcrawler_provider_{uuid4().hex[:16]}"
    admin_url = _database_url(base_url, "postgres")
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("create database {}").format(sql.Identifier(name))
        )
    return name, _database_url(base_url, name)


def _drop_database(base_url: str, name: str) -> None:
    admin_url = _database_url(base_url, "postgres")
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("drop database if exists {} with (force)").format(
                sql.Identifier(name)
            )
        )


def _execute_file(connection: psycopg.Connection, path: Path) -> None:
    with connection.cursor() as cursor:
        cursor.execute(path.read_text(), prepare=False)
    connection.commit()


def _apply_pre_provider_schema(connection: psycopg.Connection) -> None:
    _execute_file(connection, COMPATIBILITY)
    for migration in sorted(MIGRATIONS.glob("*.sql")):
        if migration.name >= TARGET.name:
            break
        _execute_file(connection, migration)


def test_provider_source_migration_declares_the_security_and_backfill_contract() -> None:
    migration = TARGET.read_text().casefold()

    for table in ("providers", "provider_items", "set_provider_items"):
        assert f"create table public.{table}" in migration
        assert f"alter table public.{table} enable row level security" in migration
        assert f"revoke all on table public.{table} from anon" in migration

    for legacy, provider in (
        ("youtube", "youtube"),
        ("soundcloud", "soundcloud"),
        ("freeteknomusic", "ftm"),
    ):
        assert f"('{legacy}', '{provider}')" in migration

    assert "unknown legacy set source" in migration
    assert "every set must have exactly one primary source link" in migration
    assert "legacy source projection mismatch" in migration
    assert "where relationship = 'source' and is_primary" in migration
    assert "grant select on table public.provider_items to anon" not in migration
    assert "grant select on table public.set_provider_items to anon" not in migration
    assert "alter table public.sets" not in migration
    assert "update public.sets" not in migration
    assert "delete from public.sets" not in migration


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is required for migration rehearsal",
)
def test_provider_source_migration_backfills_a_v02_snapshot() -> None:
    assert TEST_DATABASE_URL is not None
    database_name, database_url = _create_database(TEST_DATABASE_URL)
    try:
        with psycopg.connect(database_url) as connection:
            _apply_pre_provider_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    insert into public.sets (
                        id,
                        source,
                        source_id,
                        canonical_url,
                        title,
                        duration_seconds,
                        published_at,
                        review_status,
                        raw_payload
                    ) values
                        (
                            '00000000-0000-4000-8000-000000031001',
                            'youtube',
                            'video-23',
                            'https://www.youtube.com/watch?v=video-23',
                            'YouTube Set',
                            3600,
                            '2026-01-02T03:04:05Z',
                            'inbox',
                            '{"private_provider_value":"must-not-copy"}'::jsonb
                        ),
                        (
                            '00000000-0000-4000-8000-000000031002',
                            'soundcloud',
                            'crew/live-set',
                            'https://soundcloud.com/crew/live-set',
                            'SoundCloud Set',
                            5400,
                            null,
                            'accepted',
                            '{}'::jsonb
                        ),
                        (
                            '00000000-0000-4000-8000-000000031003',
                            'freeteknomusic',
                            'sets-23hz',
                            'https://freeteknomusic.org/sets/23hz',
                            'FTM Set',
                            null,
                            null,
                            'published',
                            '{"raw_html":"must-not-copy"}'::jsonb
                        )
                    """
                )
            connection.commit()

            _execute_file(connection, TARGET)

            with connection.cursor() as cursor:
                providers = cursor.execute(
                    """
                    select key, display_name, capabilities, enabled,
                           workload_policy, descriptor_version
                    from public.providers
                    order by key
                    """
                ).fetchall()
                assert [row[0] for row in providers] == [
                    "ftm",
                    "soundcloud",
                    "youtube",
                ]
                assert providers[0][2] == [
                    "discovery",
                    "metadata",
                    "license_evidence",
                ]
                assert providers[0][3] is False
                assert providers[2][2] == [
                    "discovery",
                    "metadata",
                    "embed",
                ]

                links = cursor.execute(
                    """
                    select
                        sets.source,
                        sets.source_id,
                        providers.key,
                        provider_items.external_id,
                        provider_items.canonical_url,
                        set_provider_items.is_primary,
                        provider_items.raw_metadata
                    from public.sets
                    join public.set_provider_items
                      on set_provider_items.set_id = sets.id
                     and set_provider_items.relationship = 'source'
                    join public.provider_items
                      on provider_items.id = set_provider_items.provider_item_id
                    join public.providers
                      on providers.id = provider_items.provider_id
                    order by sets.id
                    """
                ).fetchall()
                assert [row[:4] for row in links] == [
                    ("youtube", "video-23", "youtube", "video-23"),
                    (
                        "soundcloud",
                        "crew/live-set",
                        "soundcloud",
                        "crew/live-set",
                    ),
                    (
                        "freeteknomusic",
                        "sets-23hz",
                        "ftm",
                        "sets-23hz",
                    ),
                ]
                assert all(row[5] is True for row in links)
                assert all(
                    row[6] == {"backfilled_from_legacy": True}
                    for row in links
                )
                assert cursor.execute(
                    "select count(*) from public.sets"
                ).fetchone()[0] == 3
                assert cursor.execute(
                    "select count(*) from public.provider_items"
                ).fetchone()[0] == 3
                assert cursor.execute(
                    "select count(*) from public.set_provider_items"
                ).fetchone()[0] == 3

                with pytest.raises(UniqueViolation):
                    cursor.execute(
                        """
                        insert into public.provider_items (
                            provider_id, external_id, canonical_url
                        )
                        select id, 'video-23',
                               'https://www.youtube.com/watch?v=duplicate'
                        from public.providers where key = 'youtube'
                        """
                    )
                connection.rollback()

                with pytest.raises(UniqueViolation):
                    cursor.execute(
                        """
                        insert into public.set_provider_items (
                            set_id, provider_item_id, relationship, is_primary
                        )
                        select
                            '00000000-0000-4000-8000-000000031001',
                            provider_items.id,
                            'source',
                            true
                        from public.provider_items
                        join public.providers
                          on providers.id = provider_items.provider_id
                        where providers.key = 'soundcloud'
                        """
                    )
                connection.rollback()

                with pytest.raises(CheckViolation):
                    cursor.execute(
                        """
                        insert into public.providers (
                            key, display_name, capabilities, enabled,
                            workload_policy, descriptor_version
                        ) values (
                            'invalid',
                            'Invalid',
                            array['metadata', 'unknown'],
                            false,
                            '{}'::jsonb,
                            1
                        )
                        """
                    )
                connection.rollback()
    finally:
        _drop_database(TEST_DATABASE_URL, database_name)


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is required for migration rehearsal",
)
def test_provider_source_migration_aborts_on_unknown_legacy_source() -> None:
    assert TEST_DATABASE_URL is not None
    database_name, database_url = _create_database(TEST_DATABASE_URL)
    try:
        with psycopg.connect(database_url) as connection:
            _apply_pre_provider_schema(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    "alter table public.sets drop constraint sets_source_check"
                )
                cursor.execute(
                    """
                    insert into public.sets (
                        source, source_id, canonical_url, title, raw_payload
                    ) values (
                        'unknown-provider',
                        'unknown-23',
                        'https://unknown.example/sets/23',
                        'Unknown Set',
                        '{}'::jsonb
                    )
                    """
                )
            connection.commit()

            with pytest.raises(RaiseException, match="unknown legacy set source"):
                _execute_file(connection, TARGET)
            connection.rollback()
            with connection.cursor() as cursor:
                assert cursor.execute(
                    "select to_regclass('public.providers')"
                ).fetchone()[0] is None
    finally:
        _drop_database(TEST_DATABASE_URL, database_name)
