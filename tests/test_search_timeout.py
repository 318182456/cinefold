"""搜索超时行为测试。

单个站点卡住不应拖垮整体搜索。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas.torrent import Torrent


class _FakeSite:
    def __init__(self, name, delay=0.0, results=None, raises=False, enabled=True):
        self.name = name
        self.delay = delay
        self.results = results or []
        self.raises = raises
        self.enabled = enabled

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


class TestSearchSuccessCount:
    """空结果要能区分"站上没有"和"根本没问成"。"""

    def test_empty_but_reachable_counts_as_success(self):
        from app.modules.ptsite import search_pt_detailed

        sites = [_FakeSite("A", 0.01, []), _FakeSite("B", 0.01, [])]
        torrents, ok, total = search_pt_detailed("X", sites=sites, timeout=5)
        assert torrents == []
        assert (ok, total) == (2, 2)

    def test_failing_site_not_counted(self):
        from app.modules.ptsite import search_pt_detailed

        sites = [_FakeSite("Good", 0.01, [_torrent("Good")]),
                 _FakeSite("Bad", 0.01, raises=True)]
        torrents, ok, total = search_pt_detailed("X", sites=sites, timeout=5)
        assert [t.site for t in torrents] == ["Good"]
        assert (ok, total) == (1, 2)

    def test_all_failing_gives_zero_success(self):
        from app.modules.ptsite import search_pt_detailed

        sites = [_FakeSite("A", 0.01, raises=True), _FakeSite("B", 0.01, raises=True)]
        torrents, ok, total = search_pt_detailed("X", sites=sites, timeout=5)
        assert (torrents, ok, total) == ([], 0, 2)

    def test_disabled_site_not_counted(self):
        """未配置的站点返回空，不代表站上没有。"""
        from app.modules.ptsite import search_pt_detailed

        sites = [_FakeSite("Off", 0.01, [_torrent("Off")], enabled=False)]
        torrents, ok, total = search_pt_detailed("X", sites=sites, timeout=5)
        assert (torrents, ok, total) == ([], 0, 1)

    def test_self_reported_failure_not_counted(self):
        """站点内部吞掉异常返回空列表时，靠 search_failed 上报。"""
        from app.modules.ptsite import search_pt_detailed

        site = _FakeSite("Quiet", 0.01, [])
        site.search_failed = True
        torrents, ok, total = search_pt_detailed("X", sites=[site], timeout=5)
        assert (torrents, ok, total) == ([], 0, 1)

    def test_site_without_the_attribute_counts_as_success(self):
        """没实现 search_failed 的站点按问成处理，保持旧行为。"""
        from app.modules.ptsite import search_pt_detailed

        class _Bare:
            name = "Bare"
            enabled = True

            def search(self, keyword):
                return []

            def download_seed(self, torrent):
                return None

        torrents, ok, total = search_pt_detailed("X", sites=[_Bare()], timeout=5)
        assert (torrents, ok, total) == ([], 1, 1)


class TestEmptyResultCaching:
    """全站失败时的空结果不该被缓存成结论。"""

    def _patch_cache(self, monkeypatch):
        """拦截缓存读写，返回记录下来的写入内容。"""
        from app import services

        written = []
        monkeypatch.setattr(services, "get_rank_cache", lambda *a, **k: None)
        monkeypatch.setattr(
            services, "set_rank_cache",
            lambda ns, key, content: written.append((ns, key, content)),
        )
        return written

    def test_all_sites_failed_skips_cache(self, monkeypatch):
        from app import services

        written = self._patch_cache(monkeypatch)
        monkeypatch.setattr(
            services.ptsite, "search_pt_detailed", lambda code: ([], 0, 2)
        )
        assert services._search_pt_cached("ABC-123") == []
        assert written == []

    def test_genuinely_empty_is_cached(self, monkeypatch):
        """站点问成了、确实没有，这才是可缓存的结论。"""
        from app import services

        written = self._patch_cache(monkeypatch)
        monkeypatch.setattr(
            services.ptsite, "search_pt_detailed", lambda code: ([], 2, 2)
        )
        assert services._search_pt_cached("ABC-123") == []
        assert len(written) == 1
        assert written[0][2] == "[]"

    def test_results_are_cached(self, monkeypatch):
        from app import services

        written = self._patch_cache(monkeypatch)
        monkeypatch.setattr(
            services.ptsite, "search_pt_detailed",
            lambda code: ([_torrent("A")], 1, 2),
        )
        assert len(services._search_pt_cached("ABC-123")) == 1
        assert len(written) == 1
