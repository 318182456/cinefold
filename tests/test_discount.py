"""折扣标注。

原先只有 free 一个布尔，PERCENT_50 这类部分折扣在解析时就被压成 False，
下载完全看不出这一单要不要计一半下载量。现在多存一个 discount 字符串，
free 仍只表示「完全不计下载量」，过滤和排序照旧只认它。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.schemas.torrent import Torrent


class TestDiscountLabel:
    def test_percent_50_not_free(self):
        """50% 要标出来，但不能算 free —— 算了 only_free 就会放它过去。"""
        torrent = Torrent(discount="percent_50")
        assert torrent.discount_label == "50%"
        assert torrent.free is False

    def test_free_label(self):
        assert Torrent(discount="free", free=True).discount_label == "免费"

    def test_no_discount_is_blank(self):
        """无折扣返回空串，调用方靠它决定标不标，不能是「无」这类占位文案。"""
        assert Torrent().discount_label == ""

    def test_free_without_discount_falls_back(self):
        """自定义 BT 源只报 free，没有 discount 字段，也要标得出来。"""
        assert Torrent(free=True).discount_label == "免费"

    def test_unknown_discount_shown_as_is(self):
        """认不出的取值原样展示。标错总比无声丢掉强，还能提示要补映射。"""
        assert Torrent(discount="percent_70").discount_label == "percent_70"

    def test_roundtrip_keeps_discount(self):
        """to_dict / from_dict 要带上 discount，缓存和自定义源都走这条路。"""
        data = Torrent(discount="2x_free", free=True).to_dict()
        assert data["discount"] == "2x_free"
        assert data["discount_label"] == "2x 免费"
        assert Torrent.from_dict(data).discount == "2x_free"


class TestMTeamDiscount:
    @pytest.mark.parametrize("raw, discount, free", [
        ("FREE", "free", True),
        ("_2X_FREE", "2x_free", True),
        ("PERCENT_50", "percent_50", False),
        ("_2X_PERCENT_50", "2x_percent_50", False),
        ("PERCENT_30", "percent_30", False),
        ("NORMAL", "", False),
        ("", "", False),
        (None, "", False),
    ])
    def test_convert(self, raw, discount, free):
        from app.modules.ptsite.mteam import MTeam
        site = MTeam(api_key="k")
        torrent = site._convert(
            {"id": 1, "name": "ABP-554", "size": 1024 ** 3,
             "status": {"discount": raw, "seeders": 3}},
            "ABP-554",
        )
        assert torrent.discount == discount
        assert torrent.free is free


class TestRousiDiscount:
    @pytest.mark.parametrize("promotion, discount, free", [
        ("none", "", False),
        ("free", "free", True),
        ("double_upload_free", "2x_free", True),
        ("fifty_percent", "percent_50", False),
        ("double_upload_fifty_percent", "2x_percent_50", False),
        ("thirty_percent", "percent_30", False),
        ("double_upload", "2x", False),
    ])
    def test_normalize(self, promotion, discount, free):
        from app.modules.ptsite.rousi import _normalize_promotion
        assert _normalize_promotion(promotion) == discount

    def test_convert_sets_both_fields(self):
        from app.modules.ptsite.rousi import Rousi
        site = Rousi(apikey="pgk_test", host="https://rousi.pro")
        torrent = site._convert(
            {"id": 9539, "name": "SSIS-637", "size_bytes": 1024 ** 3,
             "seeders": 107, "promotion": "double_upload_free"},
            "SSIS-637",
        )
        assert torrent.discount == "2x_free"
        assert torrent.free is True


class TestNexusDiscount:
    @staticmethod
    def _row(html: str):
        from pyquery import PyQuery
        return PyQuery(f"<tr><td>{html}</td></tr>")

    @pytest.mark.parametrize("html, discount", [
        ("<span class='pro_free'>免费</span>", "free"),
        ("<span class='pro_50pctdown'>50%</span>", "percent_50"),
        ("<span class='pro_30pctdown'>30%</span>", "percent_30"),
        ("<span class='pro_2up'>2X</span>", "2x"),
        ("<td>普通种子</td>", ""),
    ])
    def test_extract(self, html, discount):
        from app.modules.ptsite.nexus import NexusSite
        assert NexusSite._extract_discount(self._row(html)) == discount

    def test_2up_combo_not_swallowed_by_bare_marker(self):
        """pro_free2up 里也含 pro_free，匹配顺序错了会丢掉上传翻倍那半截。"""
        from app.modules.ptsite.nexus import NexusSite
        row = self._row("<span class='pro_free2up'>2XFREE</span>")
        assert NexusSite._extract_discount(row) == "2x_free"

    def test_50pct_row_is_not_free(self):
        """整行 HTML 里 'free' 常出现在别处（如 free 相关的 JS/图标路径），
        但真正决定 free 的只能是折扣标识本身。"""
        from app.modules.ptsite.nexus import NexusSite, FREE_DISCOUNTS
        row = self._row("<span class='pro_50pctdown'>50%</span>")
        assert NexusSite._extract_discount(row) not in FREE_DISCOUNTS


class TestDownloadingMessage:
    """开始下载的通知（Telegram / 企业微信共用这一条文案）。"""

    @staticmethod
    def _send(monkeypatch, torrent):
        from app import services
        sent = []
        # 番号未必在库里，_code_display 会去查 DB，这里不关心标题
        monkeypatch.setattr(services, "_code_display", lambda code: (code, ""))
        monkeypatch.setattr(
            services.notify, "broadcast_text",
            lambda text: sent.append(text) or 1,
        )
        services.send_downloading_message("ABP-554", torrent)
        return sent[0]

    def test_free_shown(self, monkeypatch):
        torrent = Torrent(site="MTeam", size_mb=2048, seeders=30,
                          free=True, discount="free")
        assert "折扣: 免费" in self._send(monkeypatch, torrent)

    def test_percent_50_shown(self, monkeypatch):
        """50% 要照实写出来，不能因为不是 free 就当没折扣。"""
        torrent = Torrent(site="MTeam", size_mb=2048, seeders=30,
                          discount="percent_50")
        assert "折扣: 50%" in self._send(monkeypatch, torrent)

    def test_no_discount_says_none(self, monkeypatch):
        """无折扣也要明说。整段省掉的话，跟「漏解析了」在消息里分不出来。"""
        torrent = Torrent(site="MTeam", size_mb=2048, seeders=30)
        assert "折扣: 无" in self._send(monkeypatch, torrent)

    def test_no_torrent_keeps_message_short(self, monkeypatch):
        """没带种子信息时不该凭空冒出「折扣: 无」。"""
        text = self._send(monkeypatch, None)
        assert "折扣" not in text
