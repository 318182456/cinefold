"""转移做种：qBittorrent → Transmission。

重点验证三件不能出错的事：
1. tr 没接管成功时，绝不能动 qb 的任务
2. 从 qb 删任务时永远不删文件 —— 那份文件正被 tr 做种
3. 两端挂载点不同时，保存路径要按映射换算过再交给 tr
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.services import seedtransfer


class FakeQB:
    """假 qBittorrent。记录调用，不碰网络。"""

    def __init__(self, details=None, export=b"torrent-bytes", rows=None):
        self.details = details or {}
        self.export = export
        self.rows = rows or []
        self.deleted = []

    def monitor_torrent(self, hashes=None):
        return list(self.rows)

    def get_torrent_detail(self, torrent_hash):
        return self.details.get(torrent_hash)

    def export_torrent(self, torrent_hash):
        return self.export

    def delete_torrent(self, hashes, delete_files=False):
        self.deleted.append((list(hashes), delete_files))
        return list(hashes)


class FakeTR:
    """假 Transmission。"""

    def __init__(self, result="NEWHASH"):
        self.result = result
        self.added = []

    def add_torrent_for_seeding(self, content, save_path, code="", labels=None):
        self.added.append({
            "content": content,
            "save_path": save_path,
            "code": code,
            "labels": labels,
        })
        return self.result


@pytest.fixture
def wired(monkeypatch):
    """把两端下载器换成假的，并让 is_available 通过。"""
    def _wire(qb, tr):
        monkeypatch.setattr(seedtransfer, "_clients", lambda: (qb, tr))
        monkeypatch.setattr(seedtransfer, "is_available", lambda: (True, ""))
        return qb, tr
    return _wire


def _detail(torrent_hash="AAA", progress=1.0, save_path="/downloads/x", **extra):
    detail = {
        "hash": torrent_hash,
        "name": "片子一",
        "save_path": save_path,
        "content_path": save_path,
        "category": "",
        "tags": "",
        "progress": progress,
        "state": "seeding",
    }
    detail.update(extra)
    return detail


class TestTransferHashes:
    def test_transfers_completed_torrent(self, wired):
        qb, tr = wired(FakeQB(details={"AAA": _detail()}), FakeTR())

        result = seedtransfer.transfer_hashes(["AAA"], delete_source=True)

        assert result.transferred == ["AAA"]
        assert tr.added[0]["save_path"] == "/downloads/x"
        assert tr.added[0]["content"] == b"torrent-bytes"

    def test_never_deletes_files_from_source(self, wired):
        """文件正被 tr 做种，从 qb 删任务时绝不能带上文件。"""
        qb, tr = wired(FakeQB(details={"AAA": _detail()}), FakeTR())

        seedtransfer.transfer_hashes(["AAA"], delete_source=True)

        assert qb.deleted == [(["AAA"], False)]

    def test_keeps_source_when_delete_disabled(self, wired):
        qb, tr = wired(FakeQB(details={"AAA": _detail()}), FakeTR())

        result = seedtransfer.transfer_hashes(["AAA"], delete_source=False)

        assert result.transferred == ["AAA"]
        assert qb.deleted == []

    def test_source_kept_when_transmission_fails(self, wired):
        """tr 没接住就不能动 qb —— 否则这份文件谁都不做种了。"""
        qb, tr = wired(FakeQB(details={"AAA": _detail()}), FakeTR(result=None))

        result = seedtransfer.transfer_hashes(["AAA"], delete_source=True)

        assert result.transferred == []
        assert result.failed[0]["hash"] == "AAA"
        assert qb.deleted == []

    def test_skips_unfinished_torrent(self, wired):
        qb, tr = wired(FakeQB(details={"AAA": _detail(progress=0.5)}), FakeTR())

        result = seedtransfer.transfer_hashes(["AAA"], delete_source=True)

        assert result.skipped == ["AAA"]
        assert tr.added == []
        assert qb.deleted == []

    def test_export_failure_does_not_touch_source(self, wired):
        """导不出种子（qb 低于 4.5）时整条放弃，不能只删不转。"""
        qb, tr = wired(FakeQB(details={"AAA": _detail()}, export=None), FakeTR())

        result = seedtransfer.transfer_hashes(["AAA"], delete_source=True)

        assert result.transferred == []
        assert tr.added == []
        assert qb.deleted == []

    def test_missing_torrent_reported(self, wired):
        qb, tr = wired(FakeQB(details={}), FakeTR())

        result = seedtransfer.transfer_hashes(["NOPE"], delete_source=True)

        assert result.transferred == []
        assert "找不到" in result.failed[0]["reason"]

    def test_qb_delete_failure_still_counts_as_transferred(self, wired):
        """tr 已接管就算转移成功，qb 留了个残留任务不该判为失败。"""
        class Stubborn(FakeQB):
            def delete_torrent(self, hashes, delete_files=False):
                raise RuntimeError("qb 连不上")

        qb, tr = wired(Stubborn(details={"AAA": _detail()}), FakeTR())

        result = seedtransfer.transfer_hashes(["AAA"], delete_source=True)

        assert result.transferred == ["AAA"]

    def test_empty_input(self, wired):
        result = seedtransfer.transfer_hashes([])
        assert result.count == 0


class TestPathMap:
    def test_no_map_returns_original(self, monkeypatch):
        monkeypatch.setattr(
            seedtransfer, "get_settings",
            lambda: type("S", (), {"seed_transfer_path_map": ""})(),
        )
        assert seedtransfer._map_path("/downloads/x") == "/downloads/x"

    def test_prefix_replaced(self, monkeypatch):
        monkeypatch.setattr(
            seedtransfer, "get_settings",
            lambda: type("S", (), {"seed_transfer_path_map": "/downloads:/data/dl"})(),
        )
        assert seedtransfer._map_path("/downloads/x/y") == "/data/dl/x/y"

    def test_first_matching_rule_wins(self, monkeypatch):
        monkeypatch.setattr(
            seedtransfer, "get_settings",
            lambda: type("S", (), {
                "seed_transfer_path_map": "/downloads/tv:/tv, /downloads:/data/dl",
            })(),
        )
        assert seedtransfer._map_path("/downloads/tv/a") == "/tv/a"
        assert seedtransfer._map_path("/downloads/movie/a") == "/data/dl/movie/a"

    def test_unmatched_path_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            seedtransfer, "get_settings",
            lambda: type("S", (), {"seed_transfer_path_map": "/downloads:/data/dl"})(),
        )
        assert seedtransfer._map_path("/other/x") == "/other/x"

    def test_windows_drive_letter_not_split(self, monkeypatch):
        """盘符自带冒号，规则要从右边切才不会把 D: 拆坏。"""
        monkeypatch.setattr(
            seedtransfer, "get_settings",
            lambda: type("S", (), {"seed_transfer_path_map": "D:/dl:/data/dl"})(),
        )
        assert seedtransfer._map_path("D:/dl/x") == "/data/dl/x"


class TestFilter:
    def test_no_filter_accepts_all(self):
        assert seedtransfer._matches_filter(_detail(), [], [])

    def test_category_hit(self):
        detail = _detail(category="av")
        assert seedtransfer._matches_filter(detail, ["av"], [])
        assert not seedtransfer._matches_filter(detail, ["other"], [])

    def test_tag_hit(self):
        detail = _detail(tags="SSIS-001, 精品")
        assert seedtransfer._matches_filter(detail, [], ["精品"])
        assert not seedtransfer._matches_filter(detail, [], ["没有"])

    def test_either_condition_suffices(self):
        """分类和标签同时配置时，命中其一即可。"""
        detail = _detail(category="av", tags="")
        assert seedtransfer._matches_filter(detail, ["av"], ["某标签"])


class TestAutoTransfer:
    def test_disabled_does_nothing(self, monkeypatch):
        monkeypatch.setattr(
            seedtransfer, "get_settings",
            lambda: type("S", (), {"seed_transfer_enabled": False})(),
        )
        assert seedtransfer.run_auto_transfer() == 0

    def test_unconfigured_downloader_does_nothing(self, monkeypatch):
        monkeypatch.setattr(
            seedtransfer, "get_settings",
            lambda: type("S", (), {"seed_transfer_enabled": True})(),
        )
        monkeypatch.setattr(seedtransfer, "is_available", lambda: (False, "未配置"))
        assert seedtransfer.run_auto_transfer() == 0

    def test_only_completed_are_candidates(self, monkeypatch):
        rows = [
            {"hash": "AAA", "name": "完成", "completed": True},
            {"hash": "BBB", "name": "下载中", "completed": False},
        ]
        qb = FakeQB(details={"AAA": _detail()}, rows=rows)
        tr = FakeTR()

        monkeypatch.setattr(seedtransfer, "_clients", lambda: (qb, tr))
        monkeypatch.setattr(seedtransfer, "is_available", lambda: (True, ""))
        monkeypatch.setattr(
            seedtransfer, "get_settings",
            lambda: type("S", (), {
                "seed_transfer_enabled": True,
                "seed_transfer_categories": "",
                "seed_transfer_tags": "",
                "seed_transfer_delete_source": True,
                "seed_transfer_label": "t",
                "transmission_label": "",
                "seed_transfer_path_map": "",
            })(),
        )

        assert seedtransfer.run_auto_transfer() == 1
        assert [a["save_path"] for a in tr.added] == ["/downloads/x"]

    def test_batch_limit_respected(self, monkeypatch):
        rows = [
            {"hash": f"H{i}", "name": f"片{i}", "completed": True}
            for i in range(seedtransfer.BATCH_LIMIT + 5)
        ]
        qb = FakeQB(details={r["hash"]: _detail(r["hash"]) for r in rows}, rows=rows)
        tr = FakeTR()

        monkeypatch.setattr(seedtransfer, "_clients", lambda: (qb, tr))
        monkeypatch.setattr(seedtransfer, "is_available", lambda: (True, ""))
        monkeypatch.setattr(
            seedtransfer, "get_settings",
            lambda: type("S", (), {
                "seed_transfer_enabled": True,
                "seed_transfer_categories": "",
                "seed_transfer_tags": "",
                "seed_transfer_delete_source": False,
                "seed_transfer_label": "t",
                "transmission_label": "",
                "seed_transfer_path_map": "",
            })(),
        )

        assert seedtransfer.run_auto_transfer() == seedtransfer.BATCH_LIMIT


class TestAgentAction:
    def test_transfer_is_registered(self):
        from app.modules.agent.actions import ACTIONS
        assert "transfer" in ACTIONS
        # 会删源任务，不便还原，要走确认
        assert ACTIONS["transfer"][1] is True

    def test_schema_exposes_transfer(self):
        from app.modules.agent.tools import TOOL_SCHEMAS

        schema = next(
            s for s in TOOL_SCHEMAS if s["function"]["name"] == "propose_action"
        )
        actions = schema["function"]["parameters"]["properties"]["action"]["enum"]
        assert "transfer" in actions


class TestScheduler:
    def test_job_registered(self):
        from app.scheduler import INTERVAL_JOBS

        assert "transfer_seeds" in INTERVAL_JOBS
        assert INTERVAL_JOBS["transfer_seeds"]["interval_key"] == "seed_transfer_interval"


class TestTransmissionPayload:
    """交给 transmission-rpc 的种子必须是 bytes 原文。

    库只对 bytes / Path / file 做 base64；传字符串会被当作本地文件名塞进
    RPC 的 filename 字段，tr 在磁盘上找不到那个「文件」，回 invalid or
    corrupt torrent file。这两个用例守的就是参数类型。
    """

    def _client(self, recorder):
        from app.modules.downloadclient.transmission import TransmissionClient

        client = TransmissionClient.__new__(TransmissionClient)
        client.client = recorder
        client.download_path = "/downloads"
        client.label = "L"
        client._ensure_client = lambda: True
        return client

    class Recorder:
        def __init__(self):
            self.kwargs = None

        def add_torrent(self, torrent, **kwargs):
            self.kwargs = {"torrent": torrent, **kwargs}
            return type("T", (), {"hashString": "NEW"})()

        def verify_torrent(self, ids=None):
            pass

        def start_torrent(self, ids=None):
            pass

    def test_seeding_passes_raw_bytes(self):
        rec = self.Recorder()
        content = b"d8:announce4:teste"

        self._client(rec).add_torrent_for_seeding(content, save_path="/downloads/x")

        assert rec.kwargs["torrent"] == content
        assert isinstance(rec.kwargs["torrent"], bytes)

    def test_add_passes_raw_bytes(self):
        rec = self.Recorder()
        content = b"d8:announce4:teste"

        self._client(rec).add_torrent(content)

        assert rec.kwargs["torrent"] == content
        assert isinstance(rec.kwargs["torrent"], bytes)
