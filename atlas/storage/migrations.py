"""显式 schema 迁移：只做**加表 / 加列**，不做破坏性变更。

为什么需要它：`Base.metadata.create_all()` 只创建缺失的**表**，不会给已经存在的表
补列。旧库（`runs/atlas_api/*.db`、本机长期使用的库）在新代码上会直接
`no such column`。这里先用 `inspector` 读出实际列，再逐列
`ALTER TABLE ... ADD COLUMN`，并把已应用的版本记进 `schema_migrations`，
迁移可重复执行、可审计。

纪律（与设计文档 §9.3 一致）：
- 一次只加一列/一表，绝不改类型、改默认值或删列；
- 每列必须有默认值，否则旧行无法满足 NOT NULL；
- 迁移失败不吞异常：宁可启动失败，也不要带着半套 schema 继续跑。
"""

from typing import Any, Dict, List, Tuple

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

#: 当前代码期望的 schema 版本；每次改动 `ADDITIVE_COLUMNS` 就 +1。
SCHEMA_VERSION = 2

#: (表名, 列名, 列 DDL)。只允许新增列，且必须自带 DEFAULT。
ADDITIVE_COLUMNS: Tuple[Tuple[str, str, str], ...] = (
    (
        "sessions",
        "workflow_version",
        "VARCHAR(32) NOT NULL DEFAULT 'legacy_research_v1'",
    ),
)

_MIGRATION_TABLE = "schema_migrations"


def ensure_schema(engine: Engine) -> Dict[str, Any]:
    """补齐缺失的表/列；返回本次实际做了什么（供启动日志与测试断言）。"""
    added_columns: List[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    if _MIGRATION_TABLE not in existing_tables:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE TABLE {_MIGRATION_TABLE} ("
                    "version INTEGER PRIMARY KEY, applied_at VARCHAR(32) NOT NULL)"
                )
            )

    for table, column, ddl in ADDITIVE_COLUMNS:
        if table not in existing_tables:
            continue  # 表还不存在：create_all 会直接按当前模型建好
        columns = {item["name"] for item in inspect(engine).get_columns(table)}
        if column in columns:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        added_columns.append(f"{table}.{column}")

    with engine.begin() as connection:
        applied = {
            int(row[0])
            for row in connection.execute(text(f"SELECT version FROM {_MIGRATION_TABLE}"))
        }
        for version in range(1, SCHEMA_VERSION + 1):
            if version not in applied:
                connection.execute(
                    text(
                        f"INSERT INTO {_MIGRATION_TABLE} (version, applied_at) "
                        "VALUES (:version, :applied_at)"
                    ),
                    {"version": version, "applied_at": _now()},
                )

    return {
        "schema_version": SCHEMA_VERSION,
        "added_columns": added_columns,
    }


def schema_version(engine: Engine) -> int:
    """读回已应用的最高版本；没有迁移记录时返回 0。"""
    try:
        with engine.begin() as connection:
            row = connection.execute(
                text(f"SELECT MAX(version) FROM {_MIGRATION_TABLE}")
            ).first()
    except Exception:  # 表还不存在：视为尚未迁移
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
