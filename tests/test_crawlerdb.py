"""外部爬虫库（Immortal）导入。

样本取自对线上库的实测：
- movie.number 已经是规范番号（CAWD-940），Immortal 侧归一化过
- poster 是 ps.jpg 竖版小图，banner 是 pl.jpg 横版大图
- casts 是 cast.id 外键数组，cast_names 才是名字
- still_photos 会把同一批图重复两遍
- cn_title / description / duration / genres 实测全空
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.modules.crawlerdb import importer

# 实测的一行，字段名与线上一致
MOVIE_ROW = {
    "number": "CAWD-940",
    "title": "エロい身体の独身女教師をクラス全員で無責任に孕ませ犯●てみたい。 伊藤舞雪",
    "cn_title": None,
    "release_date": date(2026, 4, 3),
    "poster": "https://pics.dmm.co.jp/digital/video/cawd00940/cawd00940ps.jpg",
    "banner": "https://pics.dmm.co.jp/digital/video/cawd00940/cawd00940pl.jpg",
    # 实测重复两遍
    "still_photos": [
        "https://pics.dmm.co.jp/x/cawd00940jp-1.jpg",
        "https://pics.dmm.co.jp/x/cawd00940jp-2.jpg",
        "https://pics.dmm.co.jp/x/cawd00940jp-1.jpg",
        "https://pics.dmm.co.jp/x/cawd00940jp-2.jpg",
    ],
    "genres": [],
    "cast_names": ["伊藤舞雪"],
    "series": None,
    "producer": "kawaii",
    "publisher": "kawaii",
}

CAST_ROW = {"name": "伊藤舞雪", "cn_name": "伊藤舞雪", "photo": "https://x/avatar.jpg"}


class TestNormalizeCode:
    @pytest.mark.parametrize("raw, expected", [
        ("CAWD-940", "CAWD-940"),
        ("cawd-940", "CAWD-940"),
        ("  PRTD-035  ", "PRTD-035"),
        ("FC2-4869095", "FC2-4869095"),
        ("ssis_001", "SSIS-001"),
        ("SSIS－001", "SSIS-001"),
    ])
    def test_cleaned(self, raw, expected):
        assert importer._normalize_code(raw) == expected

    @pytest.mark.parametrize("raw", [
        "", None, "不是番号", "https://example.com/x", "12345", "CAWD",
    ])
    def test_rejected(self, raw):
        """认不出的一律丢弃。脏数据混进 Code 表比漏几条难收拾得多。"""
        assert importer._normalize_code(raw) == ""


class TestMovieRow:
    @pytest.fixture
    def item(self):
        return importer._row_to_item(MOVIE_ROW, importer.MOVIE_FIELDS)

    def test_code_and_title(self, item):
        assert item["code"] == "CAWD-940"
        assert item["title"].startswith("エロい身体")

    def test_date_object_to_string(self, item):
        assert item["release_date"] == "2026-04-03"

    def test_poster_and_banner_not_swapped(self, item):
        """poster 是 ps.jpg 竖版、banner 是 pl.jpg 横版，对调了前端会变形。"""
        assert item["poster"].endswith("ps.jpg")
        assert item["banner"].endswith("pl.jpg")

    def test_cast_names_not_ids(self, item):
        """取 cast_names 而不是 casts —— 后者是外键数组，写进去是一串数字。"""
        assert item["casts"] == "伊藤舞雪"

    def test_still_photos_deduped(self, item):
        """实测同一批图会重复两遍，原样入库等于让前端多渲染一倍。"""
        assert item["still_photo"].count("cawd00940jp-1.jpg") == 1
        assert len(item["still_photo"].split(",")) == 2

    def test_empty_fields_omitted(self, item):
        """空值不能写进去：占住字段会让本地的补全逻辑以为已经有值了。"""
        assert "cn_title" not in item     # None
        assert "genres" not in item       # 空数组
        assert "series" not in item       # None


class TestClean:
    def test_blank_string_is_none(self):
        assert importer._clean("   ") is None

    def test_empty_array_is_none(self):
        assert importer._clean([]) is None

    def test_array_of_blanks_is_none(self):
        assert importer._clean(["", "  ", None]) is None

    def test_zero_kept(self):
        """0 是有效值，不能跟空值一起被吃掉。"""
        assert importer._clean(0) == 0


class TestCastRow:
    def test_cn_name_goes_to_name_2(self):
        """本地主键是 name（日文名），中文名放 name_2（别名字段）。"""
        item = importer._row_to_item(CAST_ROW, importer.CAST_FIELDS)
        assert item["name"] == "伊藤舞雪"
        assert item["name_2"] == "伊藤舞雪"
        assert item["photo"].endswith("avatar.jpg")


class TestSQL:
    """SQL 拼装。cast 是保留字，number/id 等列名也要引号。"""

    @staticmethod
    def _capture(monkeypatch):
        seen = {}

        def _fake(sql, params=None):
            seen["sql"] = sql
            seen["params"] = params
            return []

        monkeypatch.setattr(importer, "_query", _fake)
        return seen

    def test_cast_table_quoted(self, monkeypatch):
        """cast 不加引号会被解析成 CAST(...) 语法而报错。"""
        seen = self._capture(monkeypatch)
        importer.fetch_casts()
        assert 'FROM "cast"' in seen["sql"]

    def test_since_uses_coalesce(self, monkeypatch):
        """update_time 可能为 NULL，直接比较会漏掉那些行。"""
        seen = self._capture(monkeypatch)
        importer.fetch_movies(since="2026-01-01")
        assert "coalesce(update_time, create_time)" in seen["sql"]
        assert seen["params"] == ["2026-01-01"]

    def test_no_since_no_where(self, monkeypatch):
        seen = self._capture(monkeypatch)
        importer.fetch_movies()
        assert "WHERE" not in seen["sql"]

    def test_limit_is_parameterized(self, monkeypatch):
        seen = self._capture(monkeypatch)
        importer.fetch_movies(limit=100)
        assert "LIMIT %s" in seen["sql"]
        assert seen["params"] == [100]


class TestFetchMovies:
    def test_bad_code_dropped(self, monkeypatch):
        rows = [MOVIE_ROW, {**MOVIE_ROW, "number": "这不是番号"}]
        monkeypatch.setattr(importer, "_query", lambda *a, **k: rows)
        items = importer.fetch_movies()
        assert len(items) == 1
        assert items[0]["code"] == "CAWD-940"


class TestWithoutDSN:
    """DSN 没配时不能炸，功能整体视为未启用。"""

    @pytest.fixture(autouse=True)
    def _no_dsn(self, monkeypatch):
        from app.core import config
        monkeypatch.setattr(config.get_settings(), "crawler_db_dsn", "", raising=False)

    def test_test_connection_reports_reason(self):
        ok, message = importer.test_connection()
        assert ok is False
        assert "CRAWLER_DB_DSN" in message

    def test_fetch_raises_typed_error(self):
        """抛专用异常，调用方才能把「没配置」和「查空了」分开。"""
        with pytest.raises(importer.CrawlerDBError):
            importer.fetch_movies()


class TestImportSemantics:
    def test_movies_go_through_cache_remote_codes(self, monkeypatch):
        """落库必须走 cache_remote_codes —— 只补空字段、不动 status
        这两条语义由它保证，另写一套迟早会漂。"""
        from app import services

        captured = []
        monkeypatch.setattr(importer, "fetch_movies",
                            lambda limit=0, since="": [{"code": "CAWD-940", "title": "x"}])
        monkeypatch.setattr(services, "cache_remote_codes",
                            lambda items: captured.append(items) or len(items))
        assert importer.import_movies() == 1
        # status 绝不能被导入带进来
        assert "status" not in captured[0][0]

    def test_empty_source_returns_zero(self, monkeypatch):
        monkeypatch.setattr(importer, "fetch_movies", lambda limit=0, since="": [])
        assert importer.import_movies() == 0

    def test_cast_failure_keeps_movie_result(self, monkeypatch):
        """演员导入失败不该让已经成功的番号导入白跑。"""
        monkeypatch.setattr(importer, "import_movies",
                            lambda limit=0, since="", full=False: 7)

        def _boom(limit=0):
            raise importer.CrawlerDBError("模拟失败")

        monkeypatch.setattr(importer, "import_casts", _boom)
        assert importer.import_all() == 7


class TestSchedulerTask:
    def test_skipped_without_dsn(self, monkeypatch):
        from app import scheduler
        from app.core import config
        monkeypatch.setattr(config.get_settings(), "crawler_db_dsn", "", raising=False)
        assert scheduler.import_crawler_db() == 0

    def test_connection_failure_does_not_raise(self, monkeypatch):
        """连不上只记日志返回 0。抛出去会让 APScheduler 摘掉这个任务，
        对方库恢复了也不会自己跑起来。"""
        from app import scheduler
        from app.core import config
        from app.modules import crawlerdb

        monkeypatch.setattr(config.get_settings(), "crawler_db_dsn",
                            "postgresql://x/y", raising=False)

        def _boom(*a, **k):
            raise crawlerdb.CrawlerDBError("连接爬虫库失败: 模拟")

        monkeypatch.setattr(crawlerdb, "import_all", _boom)
        assert scheduler.import_crawler_db() == 0


class TestDSNMasked:
    def test_not_returned_in_plaintext(self, monkeypatch):
        """连接串内嵌账号密码，返回给前端必须打码。"""
        from app.core import config
        settings = config.get_settings()
        monkeypatch.setattr(
            settings, "crawler_db_dsn",
            "postgresql://article:secret@192.168.3.12:5432/article", raising=False,
        )
        safe = settings.to_safe_dict()
        assert "secret" not in str(safe.get("crawler_db_dsn", ""))


class TestDuplicateCodes:
    """源库同一番号可能有多行（id 不同 number 相同，实测 OLM-332E）。

    落库端按主键逐条 session.get，查不到同批未提交的行，重复番号会变成
    两条 INSERT，commit 时撞 code_pkey 让整批回滚 —— 实测丢过 973 条。
    """

    def test_merged_not_duplicated(self, monkeypatch):
        rows = [
            {**MOVIE_ROW, "number": "OLM-332E", "series": None},
            {**MOVIE_ROW, "number": "OLM-332E", "series": "系列名"},
        ]
        monkeypatch.setattr(importer, "_query", lambda *a, **k: rows)
        items = importer.fetch_movies()
        assert len(items) == 1

    def test_merge_fills_gaps(self, monkeypatch):
        """同番号的几行往往互补，只取第一条会平白丢字段。"""
        rows = [
            {**MOVIE_ROW, "number": "OLM-332E", "series": None, "producer": "A"},
            {**MOVIE_ROW, "number": "OLM-332E", "series": "系列名", "producer": "B"},
        ]
        monkeypatch.setattr(importer, "_query", lambda *a, **k: rows)
        item = importer.fetch_movies()[0]
        assert item["series"] == "系列名"   # 第一条缺，用第二条补上
        assert item["producer"] == "A"      # 第一条有，不被覆盖

    def test_casts_deduped_too(self, monkeypatch):
        """本地 Actor 主键是 name，源表主键是 id，同名会撞 actor_pkey。"""
        rows = [
            {"name": "伊藤舞雪", "cn_name": None, "photo": "a.jpg"},
            {"name": "伊藤舞雪", "cn_name": "伊藤舞雪", "photo": None},
        ]
        monkeypatch.setattr(importer, "_query", lambda *a, **k: rows)
        items = importer.fetch_casts()
        assert len(items) == 1
        assert items[0]["photo"] == "a.jpg"
        assert items[0]["name_2"] == "伊藤舞雪"


class TestCodeFormats:
    """正则卡太严会误杀正经番号 —— 实测被丢掉 42 行。"""

    @pytest.mark.parametrize("code", [
        "T-28579",                  # 单字母前缀
        "N-1234",
        "CARIBBEANCOM-010112-001",  # 长前缀多段
        "032426-100",               # 日期式
        "HEYZO-3807",
        "FC2-4869095",
        "OLM-332E",                 # 尾部字母
        "259LUXU-1234",
    ])
    def test_accepted(self, code):
        assert importer._normalize_code(code) == code

    def test_underscore_normalized_to_hyphen(self):
        """下划线统一成横线，跟本地番号写法一致。"""
        assert importer._normalize_code("1PONDO-012345_678") == "1PONDO-012345-678"

    @pytest.mark.parametrize("code", [
        "", "不是番号", "https://example.com/x", "12345",
    ])
    def test_still_rejected(self, code):
        """放宽不等于全收，明显的脏数据仍要挡住。"""
        assert importer._normalize_code(code) == ""


class TestBatchCommit:
    """整批一个事务的话，中间一条坏数据会让全部回滚。"""

    def test_split_into_batches(self, monkeypatch):
        from app import services

        items = [{"code": f"ABP-{i:04d}", "title": "x"} for i in range(450)]
        monkeypatch.setattr(importer, "fetch_movies", lambda limit=0, since="": items)

        sizes = []
        monkeypatch.setattr(services, "cache_remote_codes",
                            lambda batch: sizes.append(len(batch)) or len(batch))
        assert importer.import_movies() == 450
        assert sizes == [200, 200, 50]

    def test_one_bad_batch_does_not_lose_the_rest(self, monkeypatch):
        """坏数据只连累同批的那几百条，其余照常入库。"""
        from app import services

        items = [{"code": f"ABP-{i:04d}", "title": "x"} for i in range(450)]
        monkeypatch.setattr(importer, "fetch_movies", lambda limit=0, since="": items)

        calls = {"n": 0}

        def _flaky(batch):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("模拟约束冲突")
            return len(batch)

        monkeypatch.setattr(services, "cache_remote_codes", _flaky)
        # 第 2 批的 200 条丢了，第 1、3 批的 250 条还在
        assert importer.import_movies() == 250


class TestWatermark:
    """增量水位：记录上次导入到哪，下次只查之后变动的行。

    比记录每个番号好在源头就少查 —— SQL 只返回变动的几条，
    而不是拉回 3428 条再本地逐个比对。
    """

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        store = {}
        monkeypatch.setattr(importer, "_read_watermark", lambda: store.get("v", ""))
        monkeypatch.setattr(importer, "_write_watermark",
                            lambda v: store.__setitem__("v", v))
        return store

    def test_first_run_is_full(self, monkeypatch, _clean):
        """没有水位记录时走全量。"""
        seen = {}
        monkeypatch.setattr(importer, "fetch_movies",
                            lambda limit=0, since="": seen.setdefault("since", since) or [])
        importer.import_movies()
        assert seen["since"] == ""

    def test_second_run_uses_watermark(self, monkeypatch, _clean):
        from app import services
        monkeypatch.setattr(importer, "fetch_movies",
                            lambda limit=0, since="": [{"code": "ABP-554"}])
        monkeypatch.setattr(services, "cache_remote_codes", lambda b: len(b))

        importer.import_movies()
        assert _clean["v"]          # 第一轮写下了水位

        seen = {}
        monkeypatch.setattr(importer, "fetch_movies",
                            lambda limit=0, since="": seen.setdefault("since", since) or [])
        importer.import_movies()
        assert seen["since"] == _clean["v"]

    def test_full_flag_ignores_watermark(self, monkeypatch, _clean):
        """full=True 时无视水位，用于重新拉一遍。"""
        _clean["v"] = "2026-01-01 00:00:00"
        seen = {}
        monkeypatch.setattr(importer, "fetch_movies",
                            lambda limit=0, since="": seen.setdefault("since", since) or [])
        importer.import_movies(full=True)
        assert seen["since"] == ""

    def test_failure_does_not_advance(self, monkeypatch, _clean):
        """有批次失败就不推进水位 —— 推了的话那些条下轮查不到，永久丢失。"""
        from app import services
        monkeypatch.setattr(importer, "fetch_movies",
                            lambda limit=0, since="": [{"code": "ABP-554"}])

        def _boom(batch):
            raise RuntimeError("模拟失败")

        monkeypatch.setattr(services, "cache_remote_codes", _boom)
        importer.import_movies()
        assert "v" not in _clean

    def test_watermark_lags_behind(self, monkeypatch, _clean):
        """水位往回退几分钟：对方入库和 update_time 落盘有时间差，
        卡着上次的时刻查会漏掉边界上那几条。"""
        from datetime import datetime

        from app import services
        monkeypatch.setattr(importer, "fetch_movies",
                            lambda limit=0, since="": [{"code": "ABP-554"}])
        monkeypatch.setattr(services, "cache_remote_codes", lambda b: len(b))

        before = datetime.now()
        importer.import_movies()
        written = datetime.strptime(_clean["v"], "%Y-%m-%d %H:%M:%S")
        gap = (before - written).total_seconds() / 60
        assert 9 <= gap <= 11


class TestDroppedLogging:
    """被跳过的番号要能从日志里看出来。

    光有「跳过 42 行」没法判断是正则卡太严误杀了正经番号，
    还是对方那边本来就有脏数据 —— 排查时第一步就是想看这些原值。
    """

    @staticmethod
    def _capture(func):
        """项目用 loguru，pytest 的 caplog 抓不到，得自己挂 sink。"""
        from loguru import logger

        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
        try:
            func()
        finally:
            logger.remove(sink)
        return "".join(lines)

    def test_bad_codes_listed(self, monkeypatch):
        rows = [
            {**MOVIE_ROW, "number": "这不是番号"},
            {**MOVIE_ROW, "number": "https://example.com/x"},
        ]
        monkeypatch.setattr(importer, "_query", lambda *a, **k: rows)
        text = self._capture(importer.fetch_movies)
        assert "这不是番号" in text
        assert "example.com" in text

    def test_long_list_truncated(self, monkeypatch):
        """脏数据很多时不能把日志刷爆。"""
        rows = [{**MOVIE_ROW, "number": f"脏数据{i}"} for i in range(80)]
        monkeypatch.setattr(importer, "_query", lambda *a, **k: rows)
        assert "另有 30 条" in self._capture(importer.fetch_movies)

    def test_blank_number_shown_as_placeholder(self, monkeypatch):
        """空番号也要占一行，否则日志里数量对不上。"""
        monkeypatch.setattr(importer, "_query",
                            lambda *a, **k: [{**MOVIE_ROW, "number": None}])
        assert "(空)" in self._capture(importer.fetch_movies)

    def test_nameless_casts_counted(self, monkeypatch):
        monkeypatch.setattr(importer, "_query",
                            lambda *a, **k: [{"name": None, "cn_name": "x", "photo": None}])
        assert "没有名字" in self._capture(importer.fetch_casts)
