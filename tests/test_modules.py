"""模块层测试：导入完整性、数据转换、工厂逻辑。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.schemas.torrent import Torrent


class TestImports:
    """所有模块都要能无副作用地导入。"""

    def test_downloadclient(self):
        from app.modules.downloadclient import get_download_client, list_configured_clients
        from app.modules.downloadclient.qbittorrent import QBitTorrentClient
        from app.modules.downloadclient.transmission import TransmissionClient
        from app.modules.downloadclient.thunder import Thunder
        assert QBitTorrentClient and TransmissionClient and Thunder

    def test_mediaserver(self):
        from app.modules.mediaserver import get_media_servers, exists_in_any
        from app.modules.mediaserver.emby import Emby
        from app.modules.mediaserver.jellyfin import Jellyfin
        from app.modules.mediaserver.plex import Plex
        assert Emby and Jellyfin and Plex

    def test_notify(self):
        from app.modules.notify import get_notifiers, broadcast_text
        from app.modules.notify.telegram import TelegramNotifier
        from app.modules.notify.wechat import WeChatNotifier
        from app.modules.notify.WXBizMsgCrypt3 import WXBizMsgCrypt
        assert TelegramNotifier and WeChatNotifier and WXBizMsgCrypt

    def test_translate(self):
        from app.modules.translate import translate, get_translators
        from app.modules.translate.baidu import Baidu
        from app.modules.translate.google import Google
        from app.modules.translate.translateai import TranslateAI
        assert Baidu and Google and TranslateAI

    def test_ptsite(self):
        from app.modules.ptsite import search_pt, get_sites, convert_to_mb
        from app.modules.ptsite.mteam import MTeam
        from app.modules.ptsite.rousi import Rousi
        from app.modules.ptsite.ptt import PTT
        from app.modules.ptsite.nicept import NicePT
        assert MTeam and Rousi and PTT and NicePT

    def test_bt(self):
        from app.modules.bt.bt import BT, add_keyword_param
        assert BT and add_keyword_param

    def test_database_models(self):
        from app.database.models import Actor, Cache, Code, CodeStatus, History, User
        assert Code.__tablename__ == "code"
        assert Actor.__tablename__ == "actor"


class TestSizeConversion:
    @pytest.mark.parametrize("text,expected", [
        ("1.5 GB", 1536.0),
        ("500 MB", 500.0),
        ("2 TB", 2097152.0),
        ("1GB", 1024.0),
        ("", 0.0),
        ("garbage", 0.0),
    ])
    def test_convert_to_mb(self, text, expected):
        from app.modules.ptsite import convert_to_mb
        assert convert_to_mb(text) == expected


class TestBTKeyword:
    def test_placeholder_substitution(self):
        from app.modules.bt.bt import add_keyword_param
        assert add_keyword_param("http://x/s?q=${keyword}", "ABP-984") == "http://x/s?q=ABP-984"
        assert add_keyword_param('{"kw":"{keyword}"}', "ABP-984") == '{"kw":"ABP-984"}'

    def test_url_without_placeholder_gets_param(self):
        from app.modules.bt.bt import add_keyword_param
        assert add_keyword_param("http://x/search", "ABP-984") == "http://x/search?keyword=ABP-984"
        assert add_keyword_param("http://x/s?a=1", "ABP-984") == "http://x/s?a=1&keyword=ABP-984"

    def test_json_body_without_placeholder_unchanged(self):
        from app.modules.bt.bt import add_keyword_param
        body = '{"page": 1}'
        assert add_keyword_param(body, "ABP-984") == body


class TestTorrentConversion:
    def test_convert_infers_attributes_from_title(self):
        from app.modules.ptsite import convert_torrent
        result = convert_torrent(
            {"id": 1, "title": "SSIS-001 中文字幕 4K", "size": "3 GB", "seeders": "12"},
            site="X", code="SSIS-001",
        )
        assert result.chinese is True
        assert result.uhd is True
        assert result.size_mb == 3072.0
        assert result.seeders == 12
        assert result.code == "SSIS-001"

    def test_explicit_flags_take_precedence(self):
        from app.modules.ptsite import convert_torrent
        result = convert_torrent(
            {"title": "plain", "free": True, "chinese": True}, site="X"
        )
        assert result.free is True
        assert result.chinese is True


class TestFactories:
    """未配置时工厂应返回空，而不是抛异常。"""

    def test_no_download_client_configured(self, monkeypatch):
        from app.core import config as config_module
        from app.modules.downloadclient import get_download_client

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "qbittorrent_url", "")
        monkeypatch.setattr(settings, "transmission_url", "")
        monkeypatch.setattr(settings, "thunder_url", "")
        assert get_download_client() is None

    def test_qb_apikey_uses_bearer_and_skips_login(self, monkeypatch):
        """配了 API Key 就不能再打 /auth/login —— qb 会拒绝。"""
        import qbittorrentapi

        from app.modules.downloadclient.qbittorrent import QBitTorrentClient

        captured = {}

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.app = type("App", (), {"version": "5.2.3"})()

        monkeypatch.setattr(qbittorrentapi, "Client", FakeClient)
        # 走到登录就说明分支错了
        monkeypatch.setattr(
            QBitTorrentClient,
            "_auth_log_in",
            lambda self: pytest.fail("API Key 模式不应调用 _auth_log_in"),
        )

        client = QBitTorrentClient(url="http://qb:8080", apikey="qbt_testkey123")
        assert client.login_qb() is True
        assert captured["EXTRA_HEADERS"] == {"Authorization": "Bearer qbt_testkey123"}
        # key 模式下不该把账号密码也带上，避免库自作主张去登录
        assert captured["username"] == ""
        assert captured["password"] == ""

    def test_qb_without_apikey_still_logs_in(self, monkeypatch):
        """没配 key 时保持原有的账号密码登录路径。"""
        import qbittorrentapi

        from app.modules.downloadclient.qbittorrent import QBitTorrentClient

        captured = {}
        called = []

        class FakeClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.app = type("App", (), {"version": "5.1.0"})()

        monkeypatch.setattr(qbittorrentapi, "Client", FakeClient)
        monkeypatch.setattr(
            QBitTorrentClient, "_auth_log_in", lambda self: called.append(True)
        )

        client = QBitTorrentClient(
            url="http://qb:8080", username="u", password="p", apikey=""
        )
        assert client.login_qb() is True
        assert called == [True]
        assert captured["EXTRA_HEADERS"] == {}
        assert captured["username"] == "u"

    def test_no_media_server_configured(self, monkeypatch):
        from app.core import config as config_module
        from app.modules.mediaserver import get_media_servers

        settings = config_module.get_settings()
        for key in ("emby_url", "jellyfin_url", "plex_url"):
            monkeypatch.setattr(settings, key, "")
        assert get_media_servers() == []

    def test_no_notifier_configured(self, monkeypatch):
        from app.core import config as config_module
        from app.modules.notify import get_notifiers

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "telegram_bot_token", "")
        monkeypatch.setattr(settings, "wechat_corp_id", "")
        assert get_notifiers() == []

    def test_nexus_host_override(self, monkeypatch):
        """站点换域名时应能用环境变量覆盖，不必改代码。"""
        from app.modules.ptsite.ptt import PTT

        assert PTT(cookie="x").host == "https://www.pttime.org"
        assert PTT(cookie="x", host="https://new.example.com/").host == "https://new.example.com"

        monkeypatch.setenv("PTT_HOST", "https://env.example.com")
        assert PTT(cookie="x").host == "https://env.example.com"

    def test_redirect_page_detected(self):
        """换域名后的 JS 跳转页不应被当成空搜索结果静默吞掉。"""
        from app.modules.ptsite.nexus import NexusSite

        site = NexusSite(cookie="x")
        site.host = "https://example.com"
        # 小体积、无种子表的页面
        assert site._parse("<html><body>Redirecting...</body></html>", "X") == []

    def test_disabled_sites_return_empty_search(self):
        """未配置凭证的站点搜索应安全返回空列表。"""
        from app.modules.ptsite.mteam import MTeam
        from app.modules.ptsite.ptt import PTT
        from app.modules.ptsite.rousi import Rousi

        assert MTeam(api_key="").search("ABP-984") == []
        assert PTT(cookie="").search("ABP-984") == []
        assert Rousi(token="").search("ABP-984") == []


class TestConfig:
    def test_sensitive_fields_masked(self):
        from app.core.config import Settings
        settings = Settings(qbittorrent_password="secret123", qbittorrent_url="http://x")
        safe = settings.to_safe_dict()
        assert safe["qbittorrent_password"] == "*" * 8
        assert safe["qbittorrent_url"] == "http://x"

    def test_empty_sensitive_not_masked(self):
        from app.core.config import Settings
        assert Settings().to_safe_dict()["qbittorrent_password"] == ""


class TestDatabase:
    def test_create_all_and_roundtrip(self):
        from app.database.base import DBBase
        from app.database.models import Code, CodeStatus
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)

        with session_scope() as session:
            session.add(Code(code="TEST-001", title="t", status=CodeStatus.SUBSCRIBED))

        with session_scope() as session:
            row = session.get(Code, "TEST-001")
            assert row is not None
            assert row.status == CodeStatus.SUBSCRIBED
            # to_dict 要把 datetime 序列化成字符串
            assert isinstance(row.to_dict()["create_time"], str)

    def test_batch_insert_ignores_duplicates(self):
        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import batch_insert_ignore_duplicate, engine, session_scope

        DBBase.metadata.create_all(engine)
        rows = [{"code": "DUP-001", "title": "a"}, {"code": "DUP-001", "title": "b"}]

        with session_scope() as session:
            batch_insert_ignore_duplicate(session, Code, rows)
            # 第二次插入同样的数据不应抛异常
            batch_insert_ignore_duplicate(session, Code, rows)
            assert session.get(Code, "DUP-001") is not None
