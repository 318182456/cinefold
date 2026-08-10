"""搜索超时行为测试。

单个站点卡住不应拖垮整体搜索。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.torrent import Torrent


class _FakeSite:
    def __init__(self, name, delay=0.0, results=None, raises=False):
        self.name = name
        self.delay = delay
        self.results = results or []
        self.raises = raises

    def search(self, keyword):
        time.sleep(self.delay)
        if self.raises:
            raise RuntimeError("站点异常")
        return self.results

    def download_seed(self, torrent):
        return None


def _torrent(name):
    return Torrent(id=1, site=name, title=f"{name} 资源", size_mb=1024, seeders=5)


class TestSearchTimeout:
    def test_fast_sites_return_normally(self):
        from app.modules.ptsite import search_pt

        sites = [
            _FakeSite("A", 0.01, [_torrent("A")]),
            _FakeSite("B", 0.01, [_torrent("B")]),
        ]
        results = search_pt("X", sites=sites, timeout=5)
        assert {t.site for t in results} == {"A", "B"}

    def test_slow_site_does_not_block_others(self):
        """慢站点超时后，仍应返回快站点的结果。"""
        from app.modules.ptsite import search_pt

        sites = [
            _FakeSite("Fast", 0.01, [_torrent("Fast")]),
            _FakeSite("Slow", 10, [_torrent("Slow")]),
        ]
        started = time.time()
        results = search_pt("X", sites=sites, timeout=1)
        elapsed = time.time() - started

        assert [t.site for t in results] == ["Fast"]
        # 应在超时附近返回，而不是等满 10 秒
        assert elapsed < 3

    def test_failing_site_is_skipped(self):
        from app.modules.ptsite import search_pt

        sites = [
            _FakeSite("Good", 0.01, [_torrent("Good")]),
            _FakeSite("Bad", 0.01, raises=True),
        ]
        results = search_pt("X", sites=sites, timeout=5)
        assert [t.site for t in results] == ["Good"]

    def test_no_sites_returns_empty(self):
        from app.modules.ptsite import search_pt

        assert search_pt("X", sites=[]) == []
