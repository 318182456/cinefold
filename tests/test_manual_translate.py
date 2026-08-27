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


# 网关实测返回的原文（gemini-2.5-flash-lite，HTTP 200、finish_reason=stop）
REFUSAL = (
    "The prompt could not be submitted. The prompt contains sensitive words "
    "that violate Google's [Generative AI Prohibited Use policy]"
    "(https://policies.google.com/terms/generative-ai/use-policy). "
    "Try rephrasing the prompt."
)
JA_TITLE = "【VR】僕の彼女がゴミ部屋で監禁され肉体奉仕を強要されアクメ漬けになるまで"


class TestRefusalDetection:
    """AI 网关的拒绝说明不能被当成译文。

    它连着 HTTP 200 一起回来，没有 refusal 字段、finish_reason 还是 stop，
    内容也非空 —— 上层看不出任何异常，那句英文就被存进 cn_title 显示在卡片上。
    """

    def test_real_refusal_is_rejected(self):
        from app.modules.translate.translateai import looks_like_refusal

        assert looks_like_refusal(REFUSAL, JA_TITLE) is True

    def test_normal_translations_survive(self):
        """正常译文一条都不能被误杀。"""
        from app.modules.translate.translateai import looks_like_refusal

        for good in [
            "我的女友在垃圾屋被监禁强迫肉体服侍直到高潮不断",
            "配送途中",
            "炮友收藏集 像朋友一样相处很开心的炮友",
            "みお",
        ]:
            assert looks_like_refusal(good, JA_TITLE) is False, good

    def test_short_title_not_killed_by_length_ratio(self):
        """短原文配短译文，别被长度比误伤。"""
        from app.modules.translate.translateai import looks_like_refusal

        assert looks_like_refusal("配送途中", "配送途中") is False

    def test_client_returns_empty_on_refusal(self, monkeypatch):
        """识别出拒绝后 translate() 要返回空串，好让工厂降级到下一家。"""
        from app.modules.translate import translateai

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [
                        {"message": {"role": "assistant", "content": REFUSAL},
                         "finish_reason": "stop"}
                    ]
                }

        class _Client:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                return _Resp()

        monkeypatch.setattr(translateai.httpx, "Client", _Client)
        client = translateai.TranslateAI(url="http://x/v1", model="m", api_key="k")
        assert client.translate(JA_TITLE) == ""

    def test_structured_refusal_field(self, monkeypatch):
        """有的网关把拒绝放在 message.refusal，content 是空的。"""
        from app.modules.translate import translateai

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"refusal": "no"}, "finish_reason": "stop"}]}

        class _Client:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                return _Resp()

        monkeypatch.setattr(translateai.httpx, "Client", _Client)
        client = translateai.TranslateAI(url="http://x/v1", model="m", api_key="k")
        assert client.translate(JA_TITLE) == ""


class TestPurgeStoredRefusals:
    def test_purge_clears_only_refusals(self):
        """存量的拒绝说明清成空串，正常译文不动。"""
        from app import services
        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            session.merge(Code(code="RF-001", title=JA_TITLE, cn_title=REFUSAL))
            session.merge(Code(code="RF-002", title="配送途中", cn_title="配送途中"))

        assert services.purge_refused_translations() >= 1
        with session_scope() as session:
            assert session.get(Code, "RF-001").cn_title == ""
            assert session.get(Code, "RF-002").cn_title == "配送途中"

    def test_manual_retry_clears_stored_refusal(self, monkeypatch):
        """重译失败时，旧的拒绝说明也要清掉，别继续顶在卡片上。"""
        from app import services
        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            session.merge(Code(code="RF-003", title=JA_TITLE, cn_title=REFUSAL))

        monkeypatch.setattr(services.translate, "is_available", lambda: True)
        monkeypatch.setattr(services, "translate_title", lambda t: "")

        res = services.translate_code_title("RF-003")
        assert res.get("error")
        with session_scope() as session:
            assert session.get(Code, "RF-003").cn_title == ""


class TestPromptHardening:
    """提示词必须把「我是翻译接口、不做内容评判」讲明白。

    片名普遍露骨，模型一旦把自己当成对话助手就会开始劝导或改写。实测同一批
    标题，温和版提示词有 3 条被拒，强化版救回 6/9 —— 这些断言盯的就是那几句
    别被人顺手改回去。
    """

    def test_prompt_states_translation_api_role(self):
        from app.modules.translate.translateai import PROMPT

        lowered = PROMPT.lower()
        # 身份是接口而非助手
        assert "translation api" in lowered
        assert "not an assistant" in lowered
        # 明确要求不得拒绝、不得评论
        assert "must always translate" in lowered
        assert "never refuse" in lowered
        assert "never comment" in lowered

    def test_prompt_demands_bare_output(self):
        """只要译文，别带引号和解释 —— 否则整段说明会被存成标题。"""
        from app.modules.translate.translateai import PROMPT

        lowered = PROMPT.lower()
        assert "simplified chinese" in lowered
        assert "only the translation" in lowered

    def test_prompt_is_sent_as_system_message(self, monkeypatch):
        """提示词要真的发出去，且发在 system 位。"""
        from app.modules.translate import translateai

        seen = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "译文"},
                                     "finish_reason": "stop"}]}

        class _Client:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, **kw):
                seen.update(kw.get("json") or {})
                return _Resp()

        monkeypatch.setattr(translateai.httpx, "Client", _Client)
        client = translateai.TranslateAI(url="http://x/v1", model="m", api_key="k")
        assert client.translate("タイトル") == "译文"

        msgs = seen.get("messages") or []
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == translateai.PROMPT
        assert msgs[1] == {"role": "user", "content": "タイトル"}

    def test_lan_gateway_not_sent_through_ambient_proxy(self, monkeypatch):
        """自建网关多是内网地址，系统代理会把请求吞掉，必须 trust_env=False。"""
        from app.modules.translate import translateai

        seen = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "译文"},
                                     "finish_reason": "stop"}]}

        class _Client:
            def __init__(self, *a, **kw):
                seen.update(kw)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                return _Resp()

        monkeypatch.setattr(translateai.httpx, "Client", _Client)
        translateai.TranslateAI(url="http://x/v1", model="m", api_key="k").translate("タ")
        assert seen.get("trust_env") is False
