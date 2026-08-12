"""核心逻辑测试：番号识别、过滤、排序。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.schemas.torrent import Torrent
from app.utils import (
    find_serial_number,
    find_serial_numbers,
    get_magnet_hash,
    get_true_code,
    is_code,
    to_cookie_dict,
)
from app.utils.codefilter import extract_subscribable_codes, filter_codes, strip_urls
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


class TestFindSerialNumbers:
    """一条消息里列多个番号是常见写法，不能只取第一个。"""

    def test_chinese_delimiter_keeps_every_code(self):
        # 顿号分隔且连写，早期正则会因为边界被吃掉而漏掉一半
        text = "我现在找到了nhdta-800、nhdta-526、nhdta-704、nhdtb-424、nhdtb-301、nhdtb-158"
        assert find_serial_numbers(text) == [
            "NHDTA-800", "NHDTA-526", "NHDTA-704",
            "NHDTB-424", "NHDTB-301", "NHDTB-158",
        ]

    def test_lowercase_without_hyphen(self):
        assert find_serial_numbers("求 jul915 谢谢") == ["JUL-915"]

    def test_deduplicates_preserving_order(self):
        assert find_serial_numbers("IPX-177 IPX-178 ipx177") == ["IPX-177", "IPX-178"]

    def test_limit_truncates(self):
        assert find_serial_numbers("abp-111 ssis-222 mide-333", limit=2) == [
            "ABP-111", "SSIS-222",
        ]

    def test_no_code_returns_empty(self):
        assert find_serial_numbers("随便聊两句") == []


class TestCodeFilter:
    def test_strip_urls_avoids_false_positive(self):
        # 链接路径里的随机串长得像番号
        assert find_serial_numbers(strip_urls("https://javdb.com/v/abc123")) == []

    def test_code_next_to_url_still_found(self):
        text = "https://javdb.com/v/xY9zQ1 jul915"
        assert find_serial_numbers(strip_urls(text)) == ["JUL-915"]

    def test_block_prefix(self):
        passed, rejected = filter_codes(
            ["NHDTB-424", "SSIS-001"], block_prefixes="NHDTB"
        )
        assert passed == ["SSIS-001"]
        assert rejected == ["NHDTB-424"]

    def test_allow_prefix_only(self):
        passed, rejected = filter_codes(
            ["NHDTB-424", "SSIS-001", "ABP-984"], allow_prefixes="nhdtb, ssis"
        )
        assert passed == ["NHDTB-424", "SSIS-001"]
        assert rejected == ["ABP-984"]

    def test_block_wins_over_allow(self):
        passed, rejected = filter_codes(
            ["SSIS-001"], allow_prefixes="SSIS", block_prefixes="SSIS"
        )
        assert passed == []
        assert rejected == ["SSIS-001"]

    def test_max_count_caps_subscriptions(self):
        codes = [f"ABP-{n}" for n in range(100, 110)]
        passed, rejected = filter_codes(codes, max_count=3)
        assert len(passed) == 3
        assert len(rejected) == 7

    def test_prefix_with_leading_digits(self):
        passed, rejected = filter_codes(
            ["259LUXU-1234"], allow_prefixes="259LUXU"
        )
        assert passed == ["259LUXU-1234"]

    def test_fc2_prefix(self):
        passed, rejected = filter_codes(
            ["FC2-PPV-1234567"], block_prefixes="FC2"
        )
        assert rejected == ["FC2-PPV-1234567"]

    def test_extract_end_to_end(self):
        text = "找到了nhdta-800、nhdtb-424 见 https://javdb.com/v/abc123"
        passed, rejected = extract_subscribable_codes(text, block_prefixes="NHDTB")
        assert passed == ["NHDTA-800"]
        assert rejected == ["NHDTB-424"]

    def test_extract_no_code(self):
        assert extract_subscribable_codes("你好") == ([], [])


class TestUpdateCheck:
    """版本比对。查不到最新版时必须静默，不能误报红点。"""

    def test_parse_version(self):
        from app.core.overlay import parse_version
        assert parse_version("2.0.4") == (2, 0, 4, 0)
        assert parse_version("v2.0.4") == (2, 0, 4, 0)
        assert parse_version("latest") is None
        assert parse_version("sha-abc123") is None
        assert parse_version("2.0") is None
        assert parse_version("") is None

    def test_parse_version_revision(self):
        """x.y.z-n 的 -n 是修订号。"""
        from app.core.overlay import parse_version
        assert parse_version("0.0.8-2") == (0, 0, 8, 2)
        assert parse_version("v0.0.8-2") == (0, 0, 8, 2)
        # 后缀必须是纯数字，semver 的 -alpha 这类仍然不收
        assert parse_version("1.2.3-alpha") is None
        assert parse_version("1.2.3-") is None
        assert parse_version("1.2.3-2-3") is None

    def test_revision_ordering(self):
        """修订号比正式版新 —— 与标准 semver 的 prerelease 相反。"""
        from app.core.overlay import parse_version as p
        assert p("0.0.8") < p("0.0.8-1") < p("0.0.8-2") < p("0.0.9")
        # 装了修订版的机器不该被镜像里的正式版判成"有更新"
        assert not p("0.0.8") > p("0.0.8-2")

    @staticmethod
    def _release(version, with_assets=True):
        assets = {}
        if with_assets:
            assets = {
                f"backend-{version}.zip": f"https://x/backend-{version}.zip",
                f"frontend-{version}.zip": f"https://x/frontend-{version}.zip",
            }
        return {"version": version, "assets": assets, "notes": "", "published_at": ""}

    def test_has_update_true(self, monkeypatch):
        from app.services import upgrade

        monkeypatch.setattr(upgrade, "APP_VERSION", "2.0.3")
        monkeypatch.setattr(
            upgrade, "fetch_latest_release", lambda: self._release("2.0.4")
        )
        result = upgrade.check_update(use_cache=False)
        assert result["has_update"] is True
        assert result["checked"] is True
        assert result["can_upgrade"] is True

    def test_no_update_when_same(self, monkeypatch):
        from app.services import upgrade

        monkeypatch.setattr(upgrade, "APP_VERSION", "2.0.4")
        monkeypatch.setattr(
            upgrade, "fetch_latest_release", lambda: self._release("2.0.4")
        )
        assert upgrade.check_update(use_cache=False)["has_update"] is False

    def test_local_newer_is_not_an_update(self, monkeypatch):
        """本地构建版本领先于发布版时不该提示。"""
        from app.services import upgrade

        monkeypatch.setattr(upgrade, "APP_VERSION", "2.1.0")
        monkeypatch.setattr(
            upgrade, "fetch_latest_release", lambda: self._release("2.0.4")
        )
        assert upgrade.check_update(use_cache=False)["has_update"] is False

    def test_unreachable_stays_silent(self, monkeypatch):
        """查不到时 checked=False，前端据此不显示红点。"""
        from app.services import upgrade

        monkeypatch.setattr(upgrade, "fetch_latest_release", lambda: {})
        result = upgrade.check_update(use_cache=False)
        assert result["checked"] is False
        assert result["has_update"] is False

    def test_unknown_local_version_stays_silent(self, monkeypatch):
        """读不到 VERSION 文件时不该把它当成"有新版"。"""
        from app.services import upgrade

        monkeypatch.setattr(upgrade, "APP_VERSION", "UNKNOWN")
        monkeypatch.setattr(
            upgrade, "fetch_latest_release", lambda: self._release("2.0.4")
        )
        assert upgrade.check_update(use_cache=False)["has_update"] is False

    def test_revision_release_is_an_update(self, monkeypatch):
        """0.0.8 → 0.0.8-2 是一次更新，包名带后缀也要能对上。"""
        from app.services import upgrade

        monkeypatch.setattr(upgrade, "APP_VERSION", "0.0.8")
        monkeypatch.setattr(
            upgrade, "fetch_latest_release", lambda: self._release("0.0.8-2")
        )
        result = upgrade.check_update(use_cache=False)
        assert result["has_update"] is True
        assert result["can_upgrade"] is True

    def test_revision_not_downgraded_by_release(self, monkeypatch):
        """装了 0.0.8-2 的机器不该被 0.0.8 判成有更新。"""
        from app.services import upgrade

        monkeypatch.setattr(upgrade, "APP_VERSION", "0.0.8-2")
        monkeypatch.setattr(
            upgrade, "fetch_latest_release", lambda: self._release("0.0.8")
        )
        assert upgrade.check_update(use_cache=False)["has_update"] is False

    def test_next_minor_beats_revision(self, monkeypatch):
        """0.0.8-2 → 0.0.9，不带后缀的正式版仍然更新。"""
        from app.services import upgrade

        monkeypatch.setattr(upgrade, "APP_VERSION", "0.0.8-2")
        monkeypatch.setattr(
            upgrade, "fetch_latest_release", lambda: self._release("0.0.9")
        )
        assert upgrade.check_update(use_cache=False)["has_update"] is True

    def test_no_assets_can_not_upgrade(self, monkeypatch):
        """只发了镜像没挂 zip 的版本：提示有更新，但不给点安装。"""
        from app.services import upgrade

        monkeypatch.setattr(upgrade, "APP_VERSION", "2.0.3")
        monkeypatch.setattr(
            upgrade,
            "fetch_latest_release",
            lambda: self._release("2.0.4", with_assets=False),
        )
        result = upgrade.check_update(use_cache=False)
        assert result["has_update"] is True
        assert result["can_upgrade"] is False

    def test_non_semver_tag_ignored(self, monkeypatch):
        """release 打了 nightly 这类标签时当作没查到，而不是拿它去比。"""
        from app.services import upgrade

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"tag_name": "nightly", "assets": []}

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, *args, **kwargs):
                return _Response()

        monkeypatch.setattr(upgrade, "_client", lambda: _Client())
        assert upgrade.fetch_latest_release() == {}

    @staticmethod
    def _client_404(list_status, list_body):
        """latest 回 404、/releases 回给定结果的假 client。"""
        class _Response:
            def __init__(self, status, body):
                self.status_code = status
                self.text = ""
                self._body = body

            def json(self):
                return self._body

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, **kwargs):
                if url.rstrip("/").endswith("/releases"):
                    return _Response(list_status, list_body)
                return _Response(404, {})

        return _Client()

    def test_404_with_zero_releases_says_so(self, monkeypatch):
        """仓库能访问但没发过 release：文案要指向发布流程，不是权限。"""
        from app.services import upgrade

        monkeypatch.setattr(
            upgrade, "_client", lambda: self._client_404(200, [])
        )
        assert upgrade.fetch_latest_release() == {}
        assert "还没有发过 release" in upgrade._last_error

    def test_404_with_existing_releases_blames_permission(self, monkeypatch):
        """/releases 有内容却 latest 404，就不是"没发过"，回到权限文案。"""
        from app.services import upgrade

        monkeypatch.setattr(
            upgrade, "_client", lambda: self._client_404(200, [{"tag_name": "v1.0.0"}])
        )
        assert upgrade.fetch_latest_release() == {}
        assert "还没有发过 release" not in upgrade._last_error
        assert "404" in upgrade._last_error

    def test_404_probe_failure_falls_back(self, monkeypatch):
        """探测请求本身失败时不能瞎猜，退回原来的兜底文案。"""
        from app.services import upgrade

        monkeypatch.setattr(
            upgrade, "_client", lambda: self._client_404(403, {})
        )
        assert upgrade.fetch_latest_release() == {}
        assert "还没有发过 release" not in upgrade._last_error

    def test_upgrade_refused_when_already_latest(self, monkeypatch):
        """当前已是最新时不该启动升级线程。"""
        from app.services import upgrade

        monkeypatch.setattr(upgrade, "APP_VERSION", "2.0.4")
        monkeypatch.setattr(
            upgrade, "fetch_latest_release", lambda: self._release("2.0.4")
        )
        started, message = upgrade.start_upgrade()
        assert started is False
        assert "无需更新" in message

    def test_upgrade_refused_without_packages(self, monkeypatch):
        """没挂 zip 的版本点安装要给出明确原因，而不是下到一半才失败。"""
        from app.services import upgrade

        monkeypatch.setattr(upgrade, "APP_VERSION", "2.0.3")
        monkeypatch.setattr(
            upgrade,
            "fetch_latest_release",
            lambda: self._release("2.0.4", with_assets=False),
        )
        started, message = upgrade.start_upgrade()
        assert started is False
        assert "未提供更新包" in message


class TestGithubProxy:
    """GitHub 加速代理。直连不通的环境全靠它，套错了更新就彻底不可用。"""

    @staticmethod
    def _settings(monkeypatch, proxy="", token="", send_token=False):
        from app.core import config as config_module

        settings = config_module.get_settings()
        monkeypatch.setattr(settings, "github_proxy", proxy)
        monkeypatch.setattr(settings, "github_token", token)
        monkeypatch.setattr(settings, "github_proxy_send_token", send_token)
        return settings

    def test_no_proxy_keeps_url(self, monkeypatch):
        from app.services import upgrade

        self._settings(monkeypatch)
        assert upgrade._proxied(upgrade.RELEASE_API) == upgrade.RELEASE_API

    def test_prefix_is_prepended(self, monkeypatch):
        from app.services import upgrade

        self._settings(monkeypatch, proxy="https://edgeone.gh-proxy.org/")
        url = "https://github.com/owner/repo/releases/download/v1/backend-1.zip"
        assert upgrade._proxied(url) == "https://edgeone.gh-proxy.org/" + url

    def test_trailing_slash_not_doubled(self, monkeypatch):
        """填不填结尾斜杠都得出同一个地址，用户不该为这个纠结。"""
        from app.services import upgrade

        with_slash = "https://edgeone.gh-proxy.org/"
        self._settings(monkeypatch, proxy=with_slash)
        expected = upgrade._proxied(upgrade.RELEASE_API)

        self._settings(monkeypatch, proxy=with_slash.rstrip("/"))
        assert upgrade._proxied(upgrade.RELEASE_API) == expected
        assert "//https" not in expected.removeprefix(with_slash)

    def test_non_github_url_untouched(self, monkeypatch):
        """自建分发的地址不该被套前缀，代理只管 GitHub。"""
        from app.services import upgrade

        self._settings(monkeypatch, proxy="https://edgeone.gh-proxy.org/")
        url = "https://mirror.example.com/backend-1.zip"
        assert upgrade._proxied(url) == url

    def test_token_withheld_from_proxy(self, monkeypatch):
        """凭证不能交给第三方代理，宁可掉回匿名限额。"""
        from app.services import upgrade

        self._settings(monkeypatch, proxy="https://edgeone.gh-proxy.org/", token="ghp_x")
        proxied = upgrade._proxied(upgrade.RELEASE_API)
        assert "Authorization" not in upgrade._api_headers(proxied)

    def test_token_sent_on_direct_connection(self, monkeypatch):
        from app.services import upgrade

        self._settings(monkeypatch, token="ghp_x")
        headers = upgrade._api_headers(upgrade._proxied(upgrade.RELEASE_API))
        assert headers["Authorization"] == "Bearer ghp_x"

    def test_token_sent_to_proxy_when_opted_in(self, monkeypatch):
        """私有仓库不带 token 就是 404，代理等于白配，得能显式打开。"""
        from app.services import upgrade

        self._settings(
            monkeypatch, proxy="https://edgeone.gh-proxy.org/",
            token="ghp_x", send_token=True,
        )
        proxied = upgrade._proxied(upgrade.RELEASE_API)
        assert upgrade._api_headers(proxied)["Authorization"] == "Bearer ghp_x"

    def test_404_without_token_blames_missing_token(self, monkeypatch):
        """私有仓库对匿名请求回 404 而不是 401，提示必须点破这一点。"""
        from app.services import upgrade

        self._settings(monkeypatch)
        assert "Token" in upgrade._explain_404(upgrade.RELEASE_API)

    def test_404_behind_proxy_blames_withheld_token(self, monkeypatch):
        from app.services import upgrade

        self._settings(
            monkeypatch, proxy="https://edgeone.gh-proxy.org/", token="ghp_x"
        )
        assert "代理携带 Token" in upgrade._explain_404(upgrade.RELEASE_API)


class TestLanNoProxy:
    """内网地址绕过代理。下载器和媒体库通常就在同一个局域网里。"""

    def test_lan_appended_when_proxy_set(self, monkeypatch):
        from app.core.config import _protect_lan_from_proxy

        monkeypatch.setenv("HTTP_PROXY", "socks5://192.168.3.12:7890")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)

        _protect_lan_from_proxy()
        assert "192.168.*" in os.environ["NO_PROXY"]
        assert "127.0.0.1" in os.environ["NO_PROXY"]

    def test_untouched_without_proxy(self, monkeypatch):
        """没配代理就别去动环境变量。"""
        from app.core.config import _protect_lan_from_proxy

        for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
            monkeypatch.delenv(key, raising=False)

        _protect_lan_from_proxy()
        assert "NO_PROXY" not in os.environ

    def test_user_entries_preserved(self, monkeypatch):
        """用户自己写的 NO_PROXY 不能被覆盖掉。"""
        from app.core.config import _protect_lan_from_proxy

        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
        monkeypatch.setenv("NO_PROXY", "example.com")

        _protect_lan_from_proxy()
        entries = os.environ["NO_PROXY"].split(",")
        assert entries[0] == "example.com"
        assert "10.*" in entries

    def test_idempotent(self, monkeypatch):
        """get_settings 会被反复调用，不能每次都往里堆重复项。"""
        from app.core.config import _protect_lan_from_proxy

        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
        monkeypatch.delenv("NO_PROXY", raising=False)

        _protect_lan_from_proxy()
        first = os.environ["NO_PROXY"]
        _protect_lan_from_proxy()
        assert os.environ["NO_PROXY"] == first


class TestOverlay:
    """热更新代码的挂载判定。装错版本比不装更糟，判定必须严格。"""

    @staticmethod
    def _make(root, version, with_app=True):
        root.mkdir(parents=True, exist_ok=True)
        if version is not None:
            (root / "VERSION").write_text(version, encoding="utf-8")
        if with_app:
            (root / "app").mkdir(exist_ok=True)
        return root

    def test_newer_overlay_wins(self, tmp_path):
        from app.core.overlay import _should_use

        image = self._make(tmp_path / "image", "0.0.7")
        overlay = self._make(tmp_path / "overlay", "0.0.8")
        assert _should_use(overlay, image)[0] is True

    def test_same_version_uses_image(self, tmp_path):
        """镜像被 pull 到与 overlay 同版本后，overlay 就该退场。"""
        from app.core.overlay import _should_use

        image = self._make(tmp_path / "image", "0.0.8")
        overlay = self._make(tmp_path / "overlay", "0.0.8")
        assert _should_use(overlay, image)[0] is False

    def test_older_overlay_ignored(self, tmp_path):
        """镜像反超 overlay：新镜像不能被旧代码盖住。"""
        from app.core.overlay import _should_use

        image = self._make(tmp_path / "image", "0.1.0")
        overlay = self._make(tmp_path / "overlay", "0.0.8")
        assert _should_use(overlay, image)[0] is False

    def test_missing_app_dir_ignored(self, tmp_path):
        """解压到一半留下的残骸不能被当成可用 overlay。"""
        from app.core.overlay import _should_use

        image = self._make(tmp_path / "image", "0.0.7")
        overlay = self._make(tmp_path / "overlay", "0.0.8", with_app=False)
        assert _should_use(overlay, image)[0] is False

    def test_unreadable_overlay_version_ignored(self, tmp_path):
        from app.core.overlay import _should_use

        image = self._make(tmp_path / "image", "0.0.7")
        overlay = self._make(tmp_path / "overlay", None)
        assert _should_use(overlay, image)[0] is False

    def test_unreadable_image_version_lets_overlay_through(self, tmp_path):
        """镜像没带 VERSION（如源码直跑）时放行，否则热更新永远失效。"""
        from app.core.overlay import _should_use

        image = self._make(tmp_path / "image", None)
        overlay = self._make(tmp_path / "overlay", "0.0.8")
        assert _should_use(overlay, image)[0] is True


class TestSafeExtract:
    """解压来自网络的 zip，路径必须当作不可信输入。"""

    def test_extracts_normal_zip(self, tmp_path):
        import zipfile
        from app.services.upgrade import _safe_extract

        archive = tmp_path / "ok.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("VERSION", "0.0.8")
            zf.writestr("app/core/x.py", "print(1)")

        dest = tmp_path / "out"
        _safe_extract(archive, dest)
        assert (dest / "VERSION").read_text() == "0.0.8"
        assert (dest / "app" / "core" / "x.py").is_file()

    def test_rejects_parent_traversal(self, tmp_path):
        import zipfile
        import pytest as _pytest
        from app.services.upgrade import _safe_extract

        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../escaped.txt", "pwned")

        with _pytest.raises(RuntimeError, match="越界"):
            _safe_extract(archive, tmp_path / "out")
        assert not (tmp_path / "escaped.txt").exists()

    def test_rejects_absolute_path(self, tmp_path):
        import zipfile
        import pytest as _pytest
        from app.services.upgrade import _safe_extract

        archive = tmp_path / "abs.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            # zipfile 会剥掉开头的 /，用 Windows 盘符形式测绝对路径
            zf.writestr("../../../etc/passwd", "x")

        with _pytest.raises(RuntimeError, match="越界"):
            _safe_extract(archive, tmp_path / "out")

    def test_rejects_zip_bomb(self, tmp_path, monkeypatch):
        import zipfile
        import pytest as _pytest
        from app.services import upgrade

        monkeypatch.setattr(upgrade, "MAX_EXTRACT_BYTES", 1024)
        archive = tmp_path / "bomb.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.bin", b"\0" * (4 * 1024))

        with _pytest.raises(RuntimeError, match="解压体积"):
            upgrade._safe_extract(archive, tmp_path / "out")


class TestVerify:
    """sha256 校验。包被截断或掉包时必须拦下。"""

    def test_matching_hash_passes(self, tmp_path):
        import hashlib
        from app.services.upgrade import _verify

        blob = tmp_path / "a.zip"
        blob.write_bytes(b"hello")
        digest = hashlib.sha256(b"hello").hexdigest()
        _verify(blob, digest, "测试包")  # 不抛即通过

    def test_mismatched_hash_raises(self, tmp_path):
        import pytest as _pytest
        from app.services.upgrade import _verify

        blob = tmp_path / "a.zip"
        blob.write_bytes(b"hello")
        with _pytest.raises(RuntimeError, match="校验失败"):
            _verify(blob, "0" * 64, "测试包")

    def test_empty_expected_skips(self, tmp_path):
        """manifest 缺失时降级放行，不能因此完全装不了。"""
        from app.services.upgrade import _verify

        blob = tmp_path / "a.zip"
        blob.write_bytes(b"hello")
        _verify(blob, "", "测试包")


class TestReplaceDir:
    """安装与回滚的目录搬运。"""

    def test_moves_backup_and_installs(self, tmp_path):
        from app.services.upgrade import _replace_dir

        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "VERSION").write_text("0.0.9")

        current = tmp_path / "current"
        current.mkdir()
        (current / "VERSION").write_text("0.0.8")

        backup = tmp_path / "backup"

        _replace_dir(staging, current, backup)
        assert (current / "VERSION").read_text() == "0.0.9"
        assert (backup / "VERSION").read_text() == "0.0.8"
        assert not staging.exists()

    def test_installs_when_no_current(self, tmp_path):
        from app.services.upgrade import _replace_dir

        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "VERSION").write_text("0.0.9")

        current = tmp_path / "current"
        _replace_dir(staging, current, tmp_path / "backup")
        assert (current / "VERSION").read_text() == "0.0.9"


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

    def test_exclude_vr(self):
        items = [_t(id=1, title="DSVR-1234 VR"), _t(id=2, title="SSIS-001 1080p")]
        result = filter_torrents(items, {"exclude_vr": True})
        assert [t.id for t in result] == [2]

    def test_only_vr(self):
        items = [_t(id=1, title="VRKM-500"), _t(id=2, title="SSIS-001")]
        assert [t.id for t in filter_torrents(items, {"only_vr": True})] == [1]

    def test_vr_not_matched_inside_words(self):
        """避免 'vr' 出现在普通单词里被误判。"""
        items = [_t(id=1, title="SSIS-001 Louvre Special")]
        assert [t.id for t in filter_torrents(items, {"exclude_vr": True})] == [1]

    def test_vr_marker_variants(self):
        """VR 系列的番号前缀补不全，主要靠标题里的标记。"""
        from app.utils.filters import has_vr

        assert has_vr("SSR-028 【VR】8KVR 女体観察")
        assert has_vr("【VR】タイトル")
        assert has_vr("8KVR 高画質")
        assert has_vr("VR専用 作品")
        assert has_vr("DSVR-1234")
        # 不该命中
        assert not has_vr("SSNI-380 絶対領域")
        assert not has_vr("SSIS-001 Louvre")

    def test_size_range_defaults_to_mb(self):
        """无单位按 MB 解析，与配置语义一致。"""
        items = [_t(id=1, size_mb=500), _t(id=2, size_mb=5000), _t(id=3, size_mb=15000)]
        result = filter_torrents(items, {"min_size": "2048", "max_size": "10240"})
        assert [t.id for t in result] == [2]

    def test_size_range_with_explicit_unit(self):
        items = [_t(id=1, size_mb=500), _t(id=2, size_mb=5000), _t(id=3, size_mb=15000)]
        result = filter_torrents(items, {"min_size": "2GB", "max_size": "10GB"})
        assert [t.id for t in result] == [2]

    def test_min_seeders(self):
        items = [_t(id=1, seeders=0), _t(id=2, seeders=5), _t(id=3, seeders=99)]
        result = filter_torrents(items, {"min_seeders": 3})
        assert [t.id for t in result] == [2, 3]

    def test_max_seeders(self):
        items = [_t(id=1, seeders=5), _t(id=2, seeders=500)]
        assert [t.id for t in filter_torrents(items, {"max_seeders": 100})] == [1]

    def test_seeders_range(self):
        items = [_t(id=1, seeders=1), _t(id=2, seeders=50), _t(id=3, seeders=999)]
        result = filter_torrents(items, {"min_seeders": 3, "max_seeders": 100})
        assert [t.id for t in result] == [2]

    def test_blank_seeders_means_unlimited(self):
        """留空、0 与非法值都不该过滤掉任何东西。"""
        items = [_t(id=1, seeders=0), _t(id=2, seeders=10)]
        for value in ("", 0, None, "abc"):
            result = filter_torrents(items, {"min_seeders": value})
            assert [t.id for t in result] == [1, 2], f"min_seeders={value!r}"

    def test_seeders_accepts_string(self):
        """前端输入框传回来的是字符串。"""
        items = [_t(id=1, seeders=1), _t(id=2, seeders=10)]
        assert [t.id for t in filter_torrents(items, {"min_seeders": "5"})] == [2]

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


class TestPrimarySite:
    """PRIMARY_SITE 决定站点优先级顺序。"""

    class _FakeSite:
        def __init__(self, name):
            self.name = name

    def _patch_sites(self, monkeypatch, names):
        sites = [self._FakeSite(n) for n in names]
        monkeypatch.setattr("app.modules.ptsite.get_sites", lambda: sites)

    def test_primary_goes_first(self, monkeypatch):
        from app import services

        self._patch_sites(monkeypatch, ["MTeam", "Rousi", "PTT"])
        monkeypatch.setattr(
            services.get_settings(), "primary_site", "PTT", raising=False
        )
        assert services.build_site_priority() == ["PTT", "MTeam", "Rousi"]

    def test_case_insensitive_match(self, monkeypatch):
        from app import services

        self._patch_sites(monkeypatch, ["MTeam", "Rousi"])
        monkeypatch.setattr(
            services.get_settings(), "primary_site", "rousi", raising=False
        )
        # 用站点自身的大小写，与 Torrent.site 对得上
        assert services.build_site_priority() == ["Rousi", "MTeam"]

    def test_multiple_primaries_keep_order(self, monkeypatch):
        from app import services

        self._patch_sites(monkeypatch, ["MTeam", "Rousi", "PTT"])
        monkeypatch.setattr(
            services.get_settings(), "primary_site", "PTT,Rousi", raising=False
        )
        assert services.build_site_priority() == ["PTT", "Rousi", "MTeam"]

    def test_empty_falls_back_to_registration_order(self, monkeypatch):
        from app import services

        self._patch_sites(monkeypatch, ["MTeam", "Rousi"])
        monkeypatch.setattr(
            services.get_settings(), "primary_site", "", raising=False
        )
        assert services.build_site_priority() == ["MTeam", "Rousi"]


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
