"""核心逻辑测试：番号识别、过滤、排序。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.schemas.torrent import Torrent
from app.utils import find_serial_number, get_magnet_hash, get_true_code, is_code, to_cookie_dict
from app.utils.filters import filter_torrents, has_chinese, has_uc, has_uhd, sort_torrents


class TestCodeRecognition:
    @pytest.mark.parametrize("text,expected", [
        ("ABP-984", "ABP-984"),
        ("abp984", "ABP-984"),
        ("SSIS-001.mp4", "SSIS-001"),
        ("[JAV] MIDE-777 1080p", "MIDE-777"),
        ("259LUXU-1234", "259LUXU-1234"),
        ("FC2-PPV-1234567", "FC2-PPV-1234567"),
        ("FC2PPV1234567", "FC2-PPV-1234567"),
        ("SSIS-001-C 中文字幕", "SSIS-001"),
        ("no code here", ""),
    ])
    def test_find_serial_number(self, text, expected):
        assert find_serial_number(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("ABP-984", True),
        ("SSIS001", True),
        ("1080P", False),
        ("", False),
    ])
    def test_is_code(self, text, expected):
        assert is_code(text) is expected

    def test_get_true_code_normalizes(self):
        assert get_true_code("abp984") == "ABP-984"
        assert get_true_code("ABP-984") == "ABP-984"
        assert get_true_code("fc2ppv1234567") == "FC2-PPV-1234567"


class TestTitleAttributes:
    def test_has_chinese(self):
        assert has_chinese("SSIS-001 中文字幕")
        assert has_chinese("MIDE-777-C")
        assert has_chinese("ABP-984 Chinese Sub")
        assert not has_chinese("SSIS-001 1080p")

    def test_has_uc(self):
        assert has_uc("SSIS-001 无码破解")
        assert has_uc("ABP-984 uncensored")
        assert not has_uc("SSIS-001 1080p")

    def test_has_uhd(self):
        assert has_uhd("SSIS-001 4K")
        assert has_uhd("ABP-984 2160p")
        assert not has_uhd("SSIS-001 1080p")


def _t(**kwargs) -> Torrent:
    base = dict(id=1, site="X", title="T", size_mb=1000.0, seeders=10)
    base.update(kwargs)
    return Torrent(**base)


class TestFilter:
    def test_only_chinese(self):
        items = [_t(id=1, title="A 中文字幕"), _t(id=2, title="B 1080p")]
        result = filter_torrents(items, {"only_chinese": True})
        assert [t.id for t in result] == [1]

    def test_exclude_uhd(self):
        items = [_t(id=1, title="A 4K"), _t(id=2, title="B 1080p")]
        result = filter_torrents(items, {"exclude_uhd": True})
        assert [t.id for t in result] == [2]

    def test_size_range_defaults_to_mb(self):
        """无单位按 MB 解析，与配置语义一致。"""
        items = [_t(id=1, size_mb=500), _t(id=2, size_mb=5000), _t(id=3, size_mb=15000)]
        result = filter_torrents(items, {"min_size": "2048", "max_size": "10240"})
        assert [t.id for t in result] == [2]

    def test_size_range_with_explicit_unit(self):
        items = [_t(id=1, size_mb=500), _t(id=2, size_mb=5000), _t(id=3, size_mb=15000)]
        result = filter_torrents(items, {"min_size": "2GB", "max_size": "10GB"})
        assert [t.id for t in result] == [2]

    def test_keywords(self):
        items = [_t(id=1, title="A leak"), _t(id=2, title="B normal")]
        assert [t.id for t in filter_torrents(items, {"include_keywords": "leak"})] == [1]
        assert [t.id for t in filter_torrents(items, {"exclude_keywords": "leak"})] == [2]

    def test_only_free(self):
        items = [_t(id=1, free=True), _t(id=2, free=False)]
        assert [t.id for t in filter_torrents(items, {"only_free": True})] == [1]

    def test_empty_config_passes_all(self):
        items = [_t(id=1), _t(id=2)]
        assert len(filter_torrents(items, {})) == 2

    def test_attributes_backfilled(self):
        """标题推断出的属性要写回对象，供排序使用。"""
        items = [_t(id=1, title="A 中文字幕 4K")]
        result = filter_torrents(items, {})
        assert result[0].chinese is True
        assert result[0].uhd is True


class TestSort:
    def test_free_first(self):
        items = [_t(id=1, free=False), _t(id=2, free=True)]
        assert [t.id for t in sort_torrents(items, "free")] == [2, 1]

    def test_multi_key_priority(self):
        # chinese 优先级高于 seeders
        items = [
            _t(id=1, chinese=False, seeders=100),
            _t(id=2, chinese=True, seeders=1),
        ]
        assert [t.id for t in sort_torrents(items, "chinese,seeders")] == [2, 1]

    def test_negated_key_demotes(self):
        """!uhd 应把非 4K 排前面。"""
        items = [_t(id=1, uhd=True), _t(id=2, uhd=False)]
        assert [t.id for t in sort_torrents(items, "!uhd")] == [2, 1]

    def test_site_priority(self):
        items = [_t(id=1, site="C"), _t(id=2, site="A"), _t(id=3, site="B")]
        result = sort_torrents(items, "site", site_priority=["A", "B", "C"])
        assert [t.site for t in result] == ["A", "B", "C"]

    def test_default_rule_end_to_end(self):
        """用项目默认排序规则跑一遍。"""
        rule = "free,chinese,uc,!uc,site,seeders,!uhd,uhd"
        items = [
            _t(id=1, free=False, chinese=False, seeders=500),
            _t(id=2, free=True, chinese=True, seeders=5),
            _t(id=3, free=True, chinese=False, seeders=50),
        ]
        # free 最优先，其次 chinese
        assert [t.id for t in sort_torrents(items, rule)] == [2, 3, 1]

    def test_empty_rule_keeps_order(self):
        items = [_t(id=1), _t(id=2)]
        assert [t.id for t in sort_torrents(items, "")] == [1, 2]


class TestMisc:
    def test_magnet_hash(self):
        magnet = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=x"
        assert get_magnet_hash(magnet) == "0123456789abcdef0123456789abcdef01234567"

    def test_magnet_hash_invalid(self):
        assert get_magnet_hash("not a magnet") == ""

    def test_cookie_dict(self):
        assert to_cookie_dict("a=1; b=2") == {"a": "1", "b": "2"}

    def test_torrent_roundtrip(self):
        original = _t(id=7, title="X", chinese=True)
        restored = Torrent.from_dict(original.to_dict())
        assert restored.id == 7 and restored.chinese is True
