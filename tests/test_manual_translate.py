"""手动翻译单个番号。

定时任务只翻 cn_title 为空的，译文落库后就永不重来 —— 机翻把标题译坏时
用户得有个办法当场要求重译，这组用例盯的就是那条路径。
"""


class TestManualTranslate:
    def _seed(self, code="MT-001", title="日文タイトル", cn=""):
        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            session.merge(Code(code=code, title=title, cn_title=cn))

    def test_overwrites_existing_translation(self, monkeypatch):
        """已有译文也要重译覆盖 —— 这正是手动按钮存在的理由。"""
        from app import services
        from app.database.models import Code
        from app.database.session import session_scope

        self._seed(cn="旧的烂译文")
        monkeypatch.setattr(services.translate, "is_available", lambda: True)
        monkeypatch.setattr(services, "translate_title", lambda t: "新译文")

        res = services.translate_code_title("MT-001")
        assert res.get("error") is None
        assert res["cn_title"] == "新译文"
        assert res["changed"] is True
        with session_scope() as session:
            assert session.get(Code, "MT-001").cn_title == "新译文"

    def test_failure_keeps_old_translation(self, monkeypatch):
        """翻译失败不能把已有译文冲成空串。"""
        from app import services
        from app.database.models import Code
        from app.database.session import session_scope

        self._seed(code="MT-002", cn="原有译文")
        monkeypatch.setattr(services.translate, "is_available", lambda: True)
        monkeypatch.setattr(services, "translate_title", lambda t: "")

        res = services.translate_code_title("MT-002")
        assert res.get("error")
        with session_scope() as session:
            assert session.get(Code, "MT-002").cn_title == "原有译文"

    def test_no_service_reports_error(self, monkeypatch):
        """没配翻译接口时说清楚，别让按钮看着像坏了。"""
        from app import services

        self._seed(code="MT-003")
        monkeypatch.setattr(services.translate, "is_available", lambda: False)
        assert "翻译服务" in services.translate_code_title("MT-003")["error"]

    def test_missing_source_title(self, monkeypatch):
        """原文为空时无从翻译，说清楚而不是静默无事发生。"""
        from app import services

        self._seed(code="MT-004", title="")
        monkeypatch.setattr(services.translate, "is_available", lambda: True)
        assert "原始标题" in services.translate_code_title("MT-004")["error"]

    def test_unknown_code(self, monkeypatch):
        from app import services

        self._seed(code="MT-005")
        monkeypatch.setattr(services.translate, "is_available", lambda: True)
        assert "不在库中" in services.translate_code_title("MT-999")["error"]

    def test_same_result_marks_unchanged(self, monkeypatch):
        """重译出一样的结果算成功，只是 changed=False。"""
        from app import services

        self._seed(code="MT-006", cn="一样的译文")
        monkeypatch.setattr(services.translate, "is_available", lambda: True)
        monkeypatch.setattr(services, "translate_title", lambda t: "一样的译文")

        res = services.translate_code_title("MT-006")
        assert res.get("error") is None
        assert res["changed"] is False
