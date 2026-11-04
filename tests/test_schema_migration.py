"""显式 schema 迁移（设计文档 §9.3）：只加表/加列，旧库打开即可用。

`Base.metadata.create_all()` 只建缺失的**表**；给已经存在的表补列必须自己动手，
否则本机已有的 `runs/atlas_api/*.db` 会在新代码上直接 `no such column`。
"""

import sqlite3

from atlas.storage import SCHEMA_VERSION, ensure_schema, schema_version
from atlas.storage.repository import GraphRepository, open_database

OLD_SESSIONS_DDL = """
CREATE TABLE sessions (
    session_id VARCHAR(64) NOT NULL PRIMARY KEY,
    goal TEXT,
    actor VARCHAR(32),
    state VARCHAR(24),
    graph_version INTEGER,
    created_at VARCHAR(32),
    updated_at VARCHAR(32)
)
"""


def _legacy_db(path):
    """造一个"旧版本"库：sessions 表没有 workflow_version，也没有 context_packages 表。"""
    connection = sqlite3.connect(path)
    connection.executescript(OLD_SESSIONS_DDL)
    connection.execute(
        "INSERT INTO sessions (session_id, goal, actor, state, graph_version,"
        " created_at, updated_at) VALUES ('s1', '旧目标', 'main', 'active', 3,"
        " '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    connection.commit()
    connection.close()


def _columns(path, table):
    connection = sqlite3.connect(path)
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def _tables(path):
    connection = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()


def test_legacy_database_gets_missing_column_and_tables(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_db(path)
    assert "workflow_version" not in _columns(path, "sessions")
    assert "context_packages" not in _tables(path)

    database = open_database(path)
    try:
        assert "workflow_version" in _columns(path, "sessions")
        assert "context_packages" in _tables(path)
        # 旧行按默认值补齐：legacy_research_v1，不伪造 DAG 计划
        repository = GraphRepository(database)
        assert repository.workflow_version("s1") == "legacy_research_v1"
        assert database.migration_report["added_columns"] == ["sessions.workflow_version"]
    finally:
        database.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "idempotent.db"
    _legacy_db(path)
    database = open_database(path)
    database.close()

    again = open_database(path)
    try:
        assert again.migration_report["added_columns"] == []
        assert schema_version(again.engine) == SCHEMA_VERSION
    finally:
        again.close()


def test_fresh_database_records_schema_version(tmp_path):
    database = open_database(tmp_path / "fresh.db")
    try:
        assert database.migration_report["added_columns"] == []
        assert schema_version(database.engine) == SCHEMA_VERSION
        # PostgreSQL 迁移前只承诺"可重复 + 只增不减"，这里断言表确实建了
        ensure_schema(database.engine)
        assert schema_version(database.engine) == SCHEMA_VERSION
    finally:
        database.close()
