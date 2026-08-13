"""把种子里的文件标记为「不需要」—— 两个下载器的实现。

合集种子删掉其中一部片时，种子本身要留着继续做种其余影片，被删的文件必须
在下载器里标记为不需要，否则下载器发现文件缺失会重新下回来。

这里不用假客户端，直接盯着两个库的真实字段名与调用参数 —— 那正是出过问题
的地方：qb 的文件序号字段叫 index，tr 的叫 id，取错会被 except 吞成
「标记失败」，症状是文件被悄悄重新下载。
"""
from __future__ import annotations

import pytest

from app.modules.downloadclient.qbittorrent import QBitTorrentClient
from app.modules.downloadclient.transmission import TransmissionClient


class _QBFile(dict):
    """仿 qbittorrentapi 的 TorrentFile：属性名是 index，没有 id。"""

    def __init__(self, index, name, priority=1):
        super().__init__(index=index, name=name, priority=priority)

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc


class _FakeQBApi:
    def __init__(self, root, names, priorities=None):
        """names 为文件相对路径；priorities 给 0 表示该文件已被标记为不需要。"""
        self.root = root
        self.names = names
        self.priorities = priorities or [1] * len(names)
        self.calls = []

    def torrents_info(self, torrent_hashes=None):
        return [type("Info", (), {"save_path": self.root})()]

    def torrents_files(self, torrent_hash=None):
        return [
            _QBFile(i, n, self.priorities[i]) for i, n in enumerate(self.names)
        ]

    def torrents_file_priority(self, torrent_hash=None, file_ids=None, priority=None):
        self.calls.append((torrent_hash, list(file_ids), priority))


def test_qb_marks_only_matching_files_unwanted(monkeypatch):
    api = _FakeQBApi("/dl", ["A/a.mp4", "A/a.nfo", "B/b.mp4"])
    client = QBitTorrentClient()
    monkeypatch.setattr(client, "_ensure_client", lambda: True)
    client.client = api

    marked, remaining = client.unwant_torrent_files(
        "HASH", ["/dl/A/a.mp4", "/dl/A/a.nfo"]
    )

    # B/b.mp4 仍需要，种子该留着继续做种
    assert (marked, remaining) == (2, 1)
    # priority=0 表示不下载；只能标记 A 的两个文件，B 不能碰
    assert api.calls == [("HASH", [0, 1], 0)]


def test_qb_reports_zero_remaining_when_all_unwanted(monkeypatch):
    """全部文件都不要了 —— 剩余数为 0，调用方据此删掉空壳种子。"""
    api = _FakeQBApi("/dl", ["A/a.mp4", "A/a.nfo"])
    client = QBitTorrentClient()
    monkeypatch.setattr(client, "_ensure_client", lambda: True)
    client.client = api

    marked, remaining = client.unwant_torrent_files(
        "HASH", ["/dl/A/a.mp4", "/dl/A/a.nfo"]
    )

    assert (marked, remaining) == (2, 0)


def test_qb_ignores_already_unwanted_files_when_counting(monkeypatch):
    """之前就标记过的文件不算「仍需要」。

    合集里的片子被逐部删除时，最后一部删完种子就该消失，而不是留下一个
    所有文件都不要的空壳。
    """
    # B 在上一次删除时已被标记为不需要（priority=0）
    api = _FakeQBApi("/dl", ["A/a.mp4", "B/b.mp4"], priorities=[1, 0])
    client = QBitTorrentClient()
    monkeypatch.setattr(client, "_ensure_client", lambda: True)
    client.client = api

    marked, remaining = client.unwant_torrent_files("HASH", ["/dl/A/a.mp4"])

    assert (marked, remaining) == (1, 0), "已标记为不需要的文件被误算成仍需要"


def test_qb_unwant_handles_no_match(monkeypatch):
    """一个都匹配不上时返回 (0,0)，不能对整个种子瞎操作。"""
    api = _FakeQBApi("/dl", ["B/b.mp4"])
    client = QBitTorrentClient()
    monkeypatch.setattr(client, "_ensure_client", lambda: True)
    client.client = api

    assert client.unwant_torrent_files("HASH", ["/dl/A/a.mp4"]) == (0, 0)
    assert api.calls == []


def test_qb_unwant_missing_torrent(monkeypatch):
    """种子已不在下载器里，返回 (0,0) 让调用方自己决定。"""
    class Empty(_FakeQBApi):
        def torrents_info(self, torrent_hashes=None):
            return []

    client = QBitTorrentClient()
    monkeypatch.setattr(client, "_ensure_client", lambda: True)
    client.client = Empty("/dl", [])

    assert client.unwant_torrent_files("GONE", ["/dl/a.mp4"]) == (0, 0)


def _tr_torrent(root, names, wanted=None):
    from transmission_rpc import Torrent
    return Torrent(fields={
        "id": 1,
        "hashString": "HASH",
        "downloadDir": root,
        "files": [{"name": n, "length": 1, "bytesCompleted": 1} for n in names],
        "priorities": [0] * len(names),
        "wanted": wanted if wanted is not None else [True] * len(names),
    })


class _FakeTRApi:
    def __init__(self, torrent):
        self.torrent = torrent
        self.calls = []

    def get_torrents(self, ids=None, arguments=None):
        self.arguments = arguments
        return [self.torrent]

    def change_torrent(self, ids=None, files_unwanted=None, **kw):
        self.calls.append((list(ids), list(files_unwanted)))


def test_tr_marks_only_matching_files_unwanted(monkeypatch):
    api = _FakeTRApi(_tr_torrent("/dl", ["A/a.mp4", "A/a.nfo", "B/b.mp4"]))
    client = TransmissionClient()
    monkeypatch.setattr(client, "_ensure_client", lambda: True)
    client.client = api

    marked, remaining = client.unwant_torrent_files(
        "HASH", ["/dl/A/a.mp4", "/dl/A/a.nfo"]
    )

    assert (marked, remaining) == (2, 1)
    assert api.calls == [(["HASH"], [0, 1])]
    # 必须显式带上 files/priorities/wanted，否则 get_files() 抛 KeyError
    for field in ("files", "priorities", "wanted"):
        assert field in api.arguments


def test_tr_reports_zero_remaining_when_all_unwanted(monkeypatch):
    api = _FakeTRApi(_tr_torrent("/dl", ["A/a.mp4", "A/a.nfo"]))
    client = TransmissionClient()
    monkeypatch.setattr(client, "_ensure_client", lambda: True)
    client.client = api

    marked, remaining = client.unwant_torrent_files(
        "HASH", ["/dl/A/a.mp4", "/dl/A/a.nfo"]
    )

    assert (marked, remaining) == (2, 0)


def test_tr_ignores_already_unwanted_files_when_counting(monkeypatch):
    """tr 的 wanted=False 等价于 qb 的 priority=0，同样不算「仍需要」。"""
    api = _FakeTRApi(_tr_torrent("/dl", ["A/a.mp4", "B/b.mp4"], wanted=[True, False]))
    client = TransmissionClient()
    monkeypatch.setattr(client, "_ensure_client", lambda: True)
    client.client = api

    marked, remaining = client.unwant_torrent_files("HASH", ["/dl/A/a.mp4"])

    assert (marked, remaining) == (1, 0), "已取消勾选的文件被误算成仍需要"


def test_tr_unwant_handles_no_match(monkeypatch):
    api = _FakeTRApi(_tr_torrent("/dl", ["B/b.mp4"]))
    client = TransmissionClient()
    monkeypatch.setattr(client, "_ensure_client", lambda: True)
    client.client = api

    assert client.unwant_torrent_files("HASH", ["/dl/A/a.mp4"]) == (0, 0)
    assert api.calls == []


@pytest.mark.parametrize("paths", [[], ["", None]])
def test_unwant_rejects_empty_input(monkeypatch, paths):
    """没给路径时不能对种子做任何操作 —— 空列表传给下载器可能被当成「全部」。"""
    for cls in (QBitTorrentClient, TransmissionClient):
        client = cls()
        monkeypatch.setattr(client, "_ensure_client", lambda: True)
        called = []
        client.client = type("Boom", (), {
            "__getattr__": lambda s, n: (lambda *a, **k: called.append(n)),
        })()
        assert client.unwant_torrent_files("HASH", paths) == (0, 0)
        assert called == [], "空输入不该触发任何下载器调用"
