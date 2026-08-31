"""AI 影评：画像聚合、结果解析、NFO 写出与渲染。

生成本身要打 AI 接口，测试里一律 mock 掉 —— conftest 已清掉 AI 凭证，
真发请求也发不出去。这里验的是拿到（或拿不到）回复之后的处理。
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from app.core.config import get_settings
from app.database.models import Code, MediaLink, Review
from app.database.session import session_scope
from app.modules.review import reviewai
from app.modules.review.profile import _aggregate, actor_profile, studio_profile
from app.services import review as service


@pytest.fixture(autouse=True)
def clean_tables():
    """建表并清掉本模块用到的三张表，避免用例相互污染。"""
    from app.database.base import DBBase
    from app.database.session import engine

    DBBase.metadata.create_all(engine)

    def _clear():
        with session_scope() as session:
            for model in (Review, MediaLink, Code):
                for row in session.query(model).all():
                    session.delete(row)

    _clear()
    yield
    _clear()


@pytest.fixture
def enabled():
    """临时打开影评开关，用完还原。"""
    settings = get_settings()
    original = settings.review_enabled
    settings.review_enabled = True
    yield settings
    settings.review_enabled = original


def _add_code(code: str, **kwargs) -> None:
    with session_scope() as session:
        session.add(Code(code=code, **kwargs))


# ----------------------------------------------------------------------
class TestCountCasts:
    """出演人数按 casts 实际条数算，不信模型。"""

    @pytest.mark.parametrize("raw,expected", [
        ("甲,乙", 2),
        ("甲、乙、丙", 3),
        ("甲/乙", 2),
        ("甲|乙", 2),
        ("甲", 1),
        ("", 0),
        (None, 0),
    ])
    def test_separators(self, raw, expected):
        assert reviewai.count_casts(raw) == expected


class TestParse:
    def test_cast_count_overrides_model(self):
        """模型给的人数不对时，以 casts 实际条数为准。"""
        raw = '{"cast_count": 1, "summary": "x"}'
        out = reviewai._parse(raw, {"casts": "甲、乙、丙"})
        assert out["cast_count"] == 3

    def test_falls_back_to_model_when_no_casts(self):
        """casts 为空时才用模型给的数。"""
        out = reviewai._parse('{"cast_count": 2}', {"casts": ""})
        assert out["cast_count"] == 2

    def test_highlights_truncated(self):
        raw = '{"highlights": ["a","b","c","d","e","f","g","h"]}'
        out = reviewai._parse(raw, {})
        assert len(out["highlights"]) == reviewai.MAX_HIGHLIGHTS

    def test_blank_highlights_dropped(self):
        out = reviewai._parse('{"highlights": ["a", "  ", ""]}', {})
        assert out["highlights"] == ["a"]

    def test_bad_json_returns_empty(self):
        assert reviewai._parse("这不是 JSON", {}) == {}

    def test_non_dict_returns_empty(self):
        assert reviewai._parse("[1, 2]", {}) == {}

    def test_strips_markdown_fence(self):
        """模型爱把 JSON 包在围栏里，即使提示词说了不要。"""
        out = reviewai._parse('```json\n{"summary": "x"}\n```', {})
        assert out["summary"] == "x"

    def test_bad_cast_count_type(self):
        out = reviewai._parse('{"cast_count": "很多"}', {"casts": ""})
        assert out["cast_count"] == 0


class TestRender:
    """给模型看的输入：空字段不出现，画像证据带命中数。"""

    def test_too_little_metadata_skipped(self):
        """只有番号时不值得发请求。"""
        assert reviewai._render({"code": "ABC-123"}, {}) == ""

    def test_empty_fields_omitted(self):
        text = reviewai._render(
            {"code": "ABC-123", "genres": "巨乳", "series": None}, {}
        )
        assert "系列" not in text
        assert "类别标签: 巨乳" in text

    def test_profile_carries_hits(self):
        profile = {
            "actors": [{
                "name": "甲", "works": 30,
                "tags": [{"tag": "巨乳", "hits": 22, "total": 30}],
            }],
            "studios": [{
                "kind": "厂牌", "name": "S1", "works": 40,
                "tags": [{"tag": "纪实", "hits": 30, "total": 40}],
            }],
        }
        text = reviewai._render(
            {"code": "ABC-123", "genres": "巨乳"}, profile
        )
        assert "巨乳 22/30" in text
        assert "纪实 30/40" in text


class TestAggregate:
    def test_low_frequency_dropped_as_noise(self):
        """只出现一次的标签多半是特例或脏数据，不能拿来概括。"""
        tags = _aggregate(["巨乳,痴女", "巨乳", "巨乳", "偶发"], 4)
        names = [t["tag"] for t in tags]
        assert "巨乳" in names
        assert "偶发" not in names

    def test_duplicate_within_one_work_counted_once(self):
        """同一部里重复的标签只算一次，否则刮削重复会灌高权重。"""
        tags = _aggregate(["巨乳,巨乳,巨乳"], 1)
        assert tags == []

    def test_empty_input(self):
        assert _aggregate([], 0) == []


class TestProfile:
    def test_actor_profile_from_history(self):
        for i, genres in enumerate(["巨乳,人妻", "巨乳,痴女", "巨乳"]):
            _add_code(f"HIS-00{i}", casts="测试演员", genres=genres)

        profile = actor_profile("测试演员")
        assert profile
        assert profile[0]["name"] == "测试演员"
        assert profile[0]["tags"][0]["tag"] == "巨乳"
        assert profile[0]["tags"][0]["hits"] == 3

    def test_actor_profile_empty_without_history(self):
        assert actor_profile("查无此人") == []
        assert actor_profile("") == []

    def test_studio_profile(self):
        for i in range(3):
            _add_code(f"STU-00{i}", producer="测试厂牌", genres="纪实,企划")

        profile = studio_profile("测试厂牌", None)
        assert profile
        assert profile[0]["kind"] == "厂牌"
        assert {t["tag"] for t in profile[0]["tags"]} == {"纪实", "企划"}

    def test_studio_profile_empty_when_unset(self):
        assert studio_profile(None, None) == []


# ----------------------------------------------------------------------
class TestMergeText:
    """AI 段可替换，官方简介必须留着。"""

    def test_keeps_original(self):
        merged = service.merge_text("官方简介", "新块")
        assert "官方简介" in merged
        assert "新块" in merged

    def test_replaces_previous_block(self):
        first = service.merge_text("官方简介", f"{service.MARKER}\n第一版")
        second = service.merge_text(first, f"{service.MARKER}\n第二版")
        assert second.count(service.MARKER) == 1
        assert "第一版" not in second
        assert "第二版" in second
        assert "官方简介" in second

    def test_empty_original(self):
        assert service.merge_text("", "块") == "块"


class TestRenderBlock:
    def test_facts_first(self):
        row = Review(
            code="ABC-123", cast_count=2, body_type="丰满",
            style="纪实", highlights="要点一\n要点二", summary="简评。",
        )
        block = service.render(row)
        lines = block.splitlines()
        assert lines[0] == service.MARKER
        assert lines[1] == "出演 2 人 / 丰满 / 纪实"
        assert "· 要点一" in block
        assert block.endswith("简评。")

    def test_empty_fields_skipped(self):
        row = Review(code="ABC-123", cast_count=0, summary="只有简评")
        block = service.render(row)
        assert "出演" not in block
        assert "只有简评" in block


class TestWriteNfo:
    def _make(self, tmp_path, body: str):
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"x")
        nfo = tmp_path / "ABC-123.nfo"
        nfo.write_text(body, encoding="utf-8")
        return video, nfo

    def test_only_plot_changed(self, tmp_path):
        """其余节点是刮削工具的成果，不能动。"""
        _, nfo = self._make(
            tmp_path,
            '<?xml version="1.0" encoding="utf-8"?>'
            "<movie><title>T</title><plot>官方简介</plot>"
            "<actor><name>甲</name></actor><genre>巨乳</genre></movie>",
        )

        with patch.object(service, "_nfo_paths", return_value=[nfo]):
            assert service._write_nfo("ABC-123", f"{service.MARKER}\n看点")

        text = nfo.read_text(encoding="utf-8")
        assert "官方简介" in text
        assert "看点" in text
        assert "<name>甲</name>" in text
        assert "<genre>巨乳</genre>" in text

    def test_rewrite_does_not_accumulate(self, tmp_path):
        _, nfo = self._make(
            tmp_path,
            "<movie><plot>官方简介</plot></movie>",
        )

        with patch.object(service, "_nfo_paths", return_value=[nfo]):
            service._write_nfo("ABC-123", f"{service.MARKER}\n第一版")
            service._write_nfo("ABC-123", f"{service.MARKER}\n第二版")

        text = nfo.read_text(encoding="utf-8")
        assert text.count(service.MARKER) == 1
        assert "第一版" not in text
        assert "第二版" in text

    def test_broken_nfo_left_untouched(self, tmp_path):
        """解析不了就别碰 —— 硬写会把整份文件毁掉。"""
        _, nfo = self._make(tmp_path, "<movie><plot>没闭合")
        before = nfo.read_text(encoding="utf-8")

        with patch.object(service, "_nfo_paths", return_value=[nfo]):
            assert service._write_nfo("ABC-123", "块") is False

        assert nfo.read_text(encoding="utf-8") == before

    def test_missing_nfo_not_created(self, tmp_path):
        """没有刮削产物时不新建，免得和后来的刮削结果打架。"""
        missing = tmp_path / "ABC-123.nfo"
        with patch.object(service, "_nfo_paths", return_value=[missing]):
            assert service._write_nfo("ABC-123", "块") is False
        assert not missing.exists()

    def test_plot_added_when_absent(self, tmp_path):
        _, nfo = self._make(tmp_path, "<movie><title>T</title></movie>")
        with patch.object(service, "_nfo_paths", return_value=[nfo]):
            service._write_nfo("ABC-123", f"{service.MARKER}\n看点")
        assert "看点" in nfo.read_text(encoding="utf-8")


# ----------------------------------------------------------------------
class TestGenerate:
    FAKE = {
        "cast_count": 1, "body_type": "丰满", "style": "纪实",
        "highlights": ["要点一"], "summary": "简评。",
    }

    def test_disabled_by_default(self):
        """开关关着时自动路径不动作。"""
        _add_code("ABC-123", genres="巨乳")
        assert service.generate_for_code("ABC-123") is False

    def test_manual_ignores_switch(self, tmp_path):
        """人点了按钮就是明确要生成，不看开关。"""
        _add_code("ABC-123", genres="巨乳", casts="甲")

        with patch("app.modules.review.build_review", return_value=self.FAKE), \
             patch.object(service, "write_out", return_value=False):
            assert service.generate_for_code("ABC-123", manual=True)

        with session_scope() as session:
            row = session.get(Review, "ABC-123")
            assert row.body_type == "丰满"
            assert row.style == "纪实"
            assert row.summary == "简评。"

    def test_skips_when_already_generated(self, enabled):
        _add_code("ABC-123", genres="巨乳")
        with session_scope() as session:
            session.add(Review(code="ABC-123", summary="旧的"))

        with patch("app.modules.review.build_review") as build:
            assert service.generate_for_code("ABC-123")
            build.assert_not_called()

    def test_force_regenerates(self, enabled):
        _add_code("ABC-123", genres="巨乳", casts="甲")
        with session_scope() as session:
            session.add(Review(code="ABC-123", summary="旧的"))

        with patch("app.modules.review.build_review", return_value=self.FAKE), \
             patch.object(service, "write_out", return_value=False):
            assert service.generate_for_code("ABC-123", force=True)

        with session_scope() as session:
            assert session.get(Review, "ABC-123").summary == "简评。"

    def test_no_metadata_skipped(self, enabled):
        """番号不在 code 表里就没得归纳。"""
        assert service.generate_for_code("NOT-EXIST") is False

    def test_empty_ai_result_not_saved(self, enabled):
        """AI 没吐出有效内容时不落库，留给下轮补漏重试。"""
        _add_code("ABC-123", genres="巨乳")

        with patch("app.modules.review.build_review", return_value={}):
            assert service.generate_for_code("ABC-123") is False

        with session_scope() as session:
            assert session.get(Review, "ABC-123") is None

    def test_write_failure_keeps_record(self, enabled):
        """写出失败不能让生成结果丢掉，定时任务会补写。"""
        _add_code("ABC-123", genres="巨乳", casts="甲")

        with patch("app.modules.review.build_review", return_value=self.FAKE), \
             patch.object(service, "write_out", side_effect=OSError("盘满了")):
            assert service.generate_for_code("ABC-123")

        with session_scope() as session:
            row = session.get(Review, "ABC-123")
            assert row is not None
            assert row.nfo_time is None


class TestFillLack:
    def test_disabled_returns_zero(self):
        assert service.fill_lack_reviews() == 0

    def test_only_codes_in_library(self, enabled):
        """没进媒体库的番号生成了也没处显示，不该被选中。"""
        _add_code("IN-LIB", genres="巨乳")
        _add_code("NOT-IN-LIB", genres="巨乳")
        with session_scope() as session:
            session.add(MediaLink(
                link_path="/lib/IN-LIB.mp4", code="IN-LIB",
                source_path="/dl/IN-LIB.mp4",
            ))

        picked = service._codes_lacking_review(10)
        assert "IN-LIB" in picked
        assert "NOT-IN-LIB" not in picked

    def test_rewrite_detects_rescrape(self, tmp_path, enabled):
        """重刮把 plot 冲掉后要能补回来。

        这类记录的 nfo_time 还留着上次写成功的时间戳，光看时间戳发现不了，
        必须真去读文件 —— 否则看点就此永久丢失。
        """
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"x")
        nfo = tmp_path / "ABC-123.nfo"
        # 刮削工具重刮后的样子：plot 是官方简介，AI 那段没了
        nfo.write_text("<movie><plot>重刮后的官方简介</plot></movie>", encoding="utf-8")

        with session_scope() as session:
            session.add(Review(
                code="ABC-123", cast_count=1, summary="看点",
                nfo_time=datetime(2025, 1, 1),
            ))
            session.add(MediaLink(
                link_path=str(video), code="ABC-123",
                source_path=str(video),
            ))

        assert service._rewrite_pending(10) == 1

        text = nfo.read_text(encoding="utf-8")
        assert service.MARKER in text
        assert "重刮后的官方简介" in text

    def test_rewrite_skips_intact_nfo(self, tmp_path, enabled):
        """NFO 里那段还在就别重复写，省掉一次无谓的读写。"""
        video = tmp_path / "ABC-123.mp4"
        video.write_bytes(b"x")
        nfo = tmp_path / "ABC-123.nfo"
        nfo.write_text(
            f"<movie><plot>官方简介\n\n{service.MARKER}\n看点</plot></movie>",
            encoding="utf-8",
        )

        with session_scope() as session:
            session.add(Review(code="ABC-123", cast_count=1, summary="看点"))
            session.add(MediaLink(
                link_path=str(video), code="ABC-123", source_path=str(video),
            ))

        assert service._rewrite_pending(10) == 0

    def test_rewrite_skips_when_not_in_library(self, enabled):
        """没进媒体库就无处可写，每轮去试只是白跑。"""
        with session_scope() as session:
            session.add(Review(code="NO-LINK", cast_count=1, summary="看点"))

        assert service._needs_rewrite("NO-LINK") is False
        assert service._rewrite_pending(10) == 0

    def test_already_reviewed_excluded(self, enabled):
        _add_code("DONE", genres="巨乳")
        with session_scope() as session:
            session.add(MediaLink(
                link_path="/lib/DONE.mp4", code="DONE",
                source_path="/dl/DONE.mp4",
            ))
            session.add(Review(code="DONE", summary="有了"))

        assert "DONE" not in service._codes_lacking_review(10)
