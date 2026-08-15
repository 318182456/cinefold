"""重复推送已存在的种子不算失败。

场景：番号已入库、种子还在下载器里做种，用户重新订阅 → 重新搜种 →
推送 → 下载器回「已存在」。目的本就是让种子在下载器里，现在它就在，
当成失败会把番号误标成下载失败，也不再跟踪它的状态。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.modules.downloadclient.qbittorrent import (
    _is_already_exists as qb_is_already_exists,
)
from app.modules.downloadclient.transmission import (
    _is_already_exists as tr_is_already_exists,
)


class Conflict409Error(Exception):
    """qbittorrent-api 的类型名。"""


class TransmissionDuplicateError(Exception):
    """transmission-rpc 的类型名。"""


class TestQbDetection:
    @pytest.mark.parametrize("exc", [
        Conflict409Error("Conflict"),
        Exception("Conflict"),
        Exception("torrent already exists"),
        Exception("409 Conflict"),
    ])
    def test_recognised(self, exc):
        assert qb_is_already_exists(exc) is True

    @pytest.mark.parametrize("exc", [
        Exception("connection refused"),
        Exception("invalid or corrupt torrent file"),
        Exception("Unauthorized"),
    ])
    def test_other_errors_still_fail(self, exc):
        assert qb_is_already_exists(exc) is False


class TestTrDetection:
    @pytest.mark.parametrize("exc", [
        TransmissionDuplicateError("duplicate torrent"),
        Exception("duplicate torrent"),
        Exception("torrent already added"),
    ])
    def test_recognised(self, exc):
        assert tr_is_already_exists(exc) is True

    @pytest.mark.parametrize("exc", [
        Exception("connection refused"),
        Exception("invalid or corrupt torrent file"),
    ])
    def test_other_errors_still_fail(self, exc):
        assert tr_is_already_exists(exc) is False


class TestQbAddReturnsHash:
    """已存在时要返回 hash，让上游照常记 History、跟踪状态。"""

    def _client(self, monkeypatch, raiser):
        from app.modules.downloadclient.qbittorrent import QBitTorrentClient

        client = QBitTorrentClient(url="http://x", username="u", password="p")

        class _Inner:
            def torrents_add(self, **kw):
                raise raiser

        client.client = _Inner()
        monkeypatch.setattr(client, "_ensure_client", lambda: True)
        return client

    def test_magnet_conflict_returns_hash(self, monkeypatch):
        magnet = "magnet:?xt=urn:btih:87e5e05065095867689532ae6f8a4a5e3ff5f366"
        client = self._client(monkeypatch, Conflict409Error("Conflict"))

        got = client.add_torrent_by_magnet(magnet, "NHDTC-079")
        assert got == "87e5e05065095867689532ae6f8a4a5e3ff5f366"

    def test_magnet_real_failure_returns_none(self, monkeypatch):
        magnet = "magnet:?xt=urn:btih:87e5e05065095867689532ae6f8a4a5e3ff5f366"
        client = self._client(monkeypatch, Exception("connection refused"))

        assert client.add_torrent_by_magnet(magnet, "NHDTC-079") is None
