"""BT 源种子下完后限制上传。

PT 站要保分享率，绝不能限；BT 源下完就没必要继续大量上传。
按 History.site 区分，老数据该列为空，一律当作「不是 BT」不动它们。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


class _FakeClient:
    """记录被限速的 hash 与限速值。"""

    def __init__(self):
        self.calls = []

    def set_upload_limit(self, hashes, limit_bytes):
        self.calls.append((sorted(hashes), limit_bytes))
        return list(hashes)


def _seed(code, torrent_hash, site):
    from app.database.base import DBBase
    from app.database.models import Code, CodeStatus, History
    from app.database.session import engine, session_scope

    DBBase.metadata.create_all(engine)
    with session_scope() as session:
        session.merge(Code(code=code, status=CodeStatus.DOWNLOADING))
        session.merge(History(hash=torrent_hash, code=code, site=site))


def _set_limit(monkeypatch, kb):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "bt_seed_upload_limit_kb", kb, raising=False)
    from app import services
    monkeypatch.setattr(services, "get_settings", lambda: settings)


class TestLimitBtUpload:
    def test_bt_torrent_is_limited(self, monkeypatch):
        from app import services

        _seed("BT-001", "hbt001", "BT")
        _set_limit(monkeypatch, 500)

        client = _FakeClient()
        services._limit_bt_upload(client, ["hbt001"])

        assert client.calls == [(["hbt001"], 500 * 1024)]

    def test_pt_torrent_untouched(self, monkeypatch):
        """PT 站的种子绝不能被限速 —— 分享率掉了账号就废了。"""
        from app import services

        _seed("PT-001", "hpt001", "MTeam")
        _set_limit(monkeypatch, 500)

        client = _FakeClient()
        services._limit_bt_upload(client, ["hpt001"])

        assert client.calls == []

    def test_legacy_rows_without_site_untouched(self, monkeypatch):
        """老数据 site 为空，不知来源就别动。"""
        from app import services

        _seed("OLD-001", "hold001", None)
        _set_limit(monkeypatch, 500)

        client = _FakeClient()
        services._limit_bt_upload(client, ["hold001"])

        assert client.calls == []

    def test_zero_limit_disables_feature(self, monkeypatch):
        """限速值为 0 表示不启用，一次下载器调用都不该发生。"""
        from app import services

        _seed("BT-002", "hbt002", "BT")
        _set_limit(monkeypatch, 0)

        client = _FakeClient()
        services._limit_bt_upload(client, ["hbt002"])

        assert client.calls == []

    def test_mixed_batch_only_limits_bt(self, monkeypatch):
        """同一批里 PT 与 BT 混着，只限 BT 那个。"""
        from app import services

        _seed("BT-003", "hbt003", "BT")
        _seed("PT-003", "hpt003", "NicePT")
        _set_limit(monkeypatch, 200)

        client = _FakeClient()
        services._limit_bt_upload(client, ["hbt003", "hpt003"])

        assert client.calls == [(["hbt003"], 200 * 1024)]

    def test_client_without_support_is_skipped(self, monkeypatch):
        """迅雷没实现这个接口，不能因此炸掉。"""
        from app import services

        _seed("BT-004", "hbt004", "BT")
        _set_limit(monkeypatch, 500)

        class _NoSupport:
            pass

        # 不抛异常即为通过
        services._limit_bt_upload(_NoSupport(), ["hbt004"])

    def test_setter_failure_is_swallowed(self, monkeypatch):
        """限速失败不该影响下载完成的状态更新与通知。"""
        from app import services

        _seed("BT-005", "hbt005", "BT")
        _set_limit(monkeypatch, 500)

        class _Broken:
            def set_upload_limit(self, hashes, limit_bytes):
                raise RuntimeError("下载器挂了")

        services._limit_bt_upload(_Broken(), ["hbt005"])


class TestSyncWiring:
    """单测 _limit_bt_upload 覆盖不到接线 —— 调用被摘掉时那些用例照样全绿。"""

    def test_sync_download_status_triggers_limit(self, monkeypatch):
        from app import services
        from app.database.base import DBBase
        from app.database.models import Code, CodeStatus, History
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        code, torrent_hash = "WIRE-001", "hwire001"
        with session_scope() as session:
            session.merge(Code(code=code, status=CodeStatus.DOWNLOADING))
            session.merge(History(hash=torrent_hash, code=code, site="BT"))

        class _Client:
            def monitor_torrent(self, hashes=None):
                return [{"hash": torrent_hash, "completed": True, "save_path": "/x"}]

        monkeypatch.setattr(
            services.downloadclient, "get_download_client", lambda name="": _Client(),
        )
        monkeypatch.setattr(services, "send_downloaded_message", lambda c: None)
        monkeypatch.setattr(services, "_transfer_just_completed", lambda cl, hs: None)

        handed = []
        monkeypatch.setattr(
            services, "_limit_bt_upload", lambda cl, hs: handed.append(list(hs)),
        )

        assert services.sync_download_status() == 1
        assert handed == [[torrent_hash]]
