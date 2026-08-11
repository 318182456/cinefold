"""迁移时的 actor 行过滤。

老版本的 actor 表是演员资料缓存（抓番号时顺手存下的演员名和头像），
新版把同一张表重新定义成了订阅表。整表搬过来会让几百条资料记录
变成"已订阅"，演员订阅任务每轮都拿它们去刷番号。
真实订阅一定带 limit_date，靠这一列区分两种语义。
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, inspect

from app.database.models import Actor
from app.database.utils import migrate as mig


def _make_source(path, *, with_limit_date: bool, rows: list[tuple]) -> None:
    """造一个老库。with_limit_date=False 模拟没有该列的更老版本。"""
    conn = sqlite3.connect(path)
    if with_limit_date:
        conn.execute(
            "CREATE TABLE actor ("
            "name VARCHAR(128) PRIMARY KEY, name_2 VARCHAR(255), photo TEXT,"
            "limit_date VARCHAR(32), create_time DATETIME, update_time DATETIME)"
        )
        conn.executemany(
            "INSERT INTO actor (name, photo, limit_date) VALUES (?, ?, ?)", rows
        )
    else:
        conn.execute(
            "CREATE TABLE actor ("
            "name VARCHAR(128) PRIMARY KEY, name_2 VARCHAR(255), photo TEXT,"
            "create_time DATETIME, update_time DATETIME)"
        )
        conn.executemany("INSERT INTO actor (name, photo) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def _kept_names(db_path) -> list[str]:
    """跑一遍取数流程，返回实际会被搬过去的演员名。"""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        source_columns = {c["name"] for c in inspect(engine).get_columns("actor")}
        columns = [c.name for c in Actor.__table__.columns if c.name in source_columns]
        names = []
        for batch in mig._iter_batches(engine, Actor, columns):
            names.extend(row["name"] for row in batch)
        return names
    finally:
        engine.dispose()


def test_只搬带起始日期的演员(tmp_path):
    db = tmp_path / "old.db"
    _make_source(db, with_limit_date=True, rows=[
        ("真实订阅", "http://x/1.jpg", "2024-01-01"),
        ("资料缓存A", "http://x/now_printing.jpg", None),
        ("资料缓存B", "http://x/now_printing.jpg", ""),
    ])

    assert _kept_names(db) == ["真实订阅"]


def test_全是资料缓存时一行都不搬(tmp_path):
    db = tmp_path / "old.db"
    _make_source(db, with_limit_date=True, rows=[
        ("茜さな", None, None),
        ("水瀬りた", None, None),
    ])

    assert _kept_names(db) == []


def test_老库缺列时整表跳过(tmp_path):
    """没有 limit_date 列就无从区分语义，不能赌，整表跳过。"""
    db = tmp_path / "veryold.db"
    _make_source(db, with_limit_date=False, rows=[("茜さな", None)])

    engine = create_engine(f"sqlite:///{db}")
    try:
        source_columns = {c["name"] for c in inspect(engine).get_columns("actor")}
        missing = mig.REQUIRED_COLUMNS["actor"] - source_columns
        assert missing == {"limit_date"}
    finally:
        engine.dispose()

    # 预览也要显示 0，不能让界面报几百行而实际搬 0 行
    assert mig._peek_tables(db).get("actor") == 0


def test_预览行数与实际搬运一致(tmp_path):
    db = tmp_path / "old.db"
    _make_source(db, with_limit_date=True, rows=[
        ("订阅1", None, "2024-01-01"),
        ("订阅2", None, "2025-06-01"),
        ("缓存", None, None),
    ])

    assert mig._peek_tables(db).get("actor") == 2
    assert len(_kept_names(db)) == 2


def test_其他表不受行过滤影响(tmp_path):
    """行过滤是表级白名单，别误伤 code/history。"""
    assert set(mig.ROW_FILTERS) == {"actor"}
    assert set(mig.REQUIRED_COLUMNS) == {"actor"}
