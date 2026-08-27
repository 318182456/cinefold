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


class TestSilentTruncation:
    """网关的输出侧过滤会把译文从中间砍断，且伪装成正常结束。

    这是比拒绝更阴的一种失败：finish_reason 还是 stop、usage 的
    completion_tokens 与返回字数完全对得上（模型确实"只生成了那么多"），
    长度比也落在正常译文的区间内 —— 没有任何字段能把它和完整译文分开。

    所以不能靠检测兜住它，只能换一个不做这种过滤的模型（Gemini 会，
    Claude 不会）。这条用例守的是：真出现半截译文时，别自作聪明地
    "修补"或猜测，宁可当失败处理。
    """

    def test_truncated_output_is_not_repaired(self, monkeypatch):
        """半截译文不做拼接猜测 —— 悄悄译残比翻不出来更糟，它看着是对的。"""
        from app.modules.translate import translateai

        # 实测 gemini-2.5-flash-lite 对这个标题的返回：从"我的女友在"就断了
        half = "【VR】我的女友在"

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": half}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 6, "total_tokens": 50},
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
        # 原样返回，不拼接、不补全、不重试拼凑
        assert client.translate(JA_TITLE) == half

    def test_refusal_still_beats_truncation_check(self):
        """拒绝说明仍要被拦下 —— 别因为放过截断就把拒绝也放过去了。"""
        from app.modules.translate.translateai import looks_like_refusal

        assert looks_like_refusal(REFUSAL, JA_TITLE) is True
        assert looks_like_refusal("【VR】我的女友在", JA_TITLE) is False


class TestGoogleBillingRefusal:
    """Google 翻译没有免费额度，项目没绑结算账号时每次调用都 403。

    它的文案是「User Rate Limit Exceeded」，看着像临时限流，实际重试多少次
    都一样（实测单字符、间隔 3 秒重试，一律 403）。判据是免费的 languages
    端点返回 200 而计费的 translate/detect 一律 403 —— key 有效、API 已启用，
    纯粹是没开通结算。
    """

    def _client(self, monkeypatch, status, payload=None):
        from app.modules.translate import google as gmod

        class _Resp:
            status_code = status

            def raise_for_status(self):
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}")

            def json(self):
                return payload or {}

        class _Client:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                return _Resp()

        monkeypatch.setattr(gmod.httpx, "Client", _Client)
        gmod.Google._quota_warned = False
        return gmod.Google(api_key="k")

    def test_403_returns_empty_for_fallback(self, monkeypatch):
        """403 要返回空串，好让工厂降级到下一家，而不是抛出去。"""
        g = self._client(monkeypatch, 403)
        assert g.translate("配送途中") == ""

    def test_billing_warning_logged_once(self, monkeypatch):
        """45000 条番号不能每条刷一行告警。"""
        from app.modules.translate import google as gmod

        g = self._client(monkeypatch, 403)
        seen = []
        monkeypatch.setattr(gmod.logger, "warning", lambda m: seen.append(m))

        for _ in range(5):
            g.translate("配送途中")

        assert len(seen) == 1
        # 得说清楚是结算问题，别让人以为是限流去傻等
        assert "结算" in seen[0]

    def test_success_path_unaffected(self, monkeypatch):
        """正常 200 照旧取译文。"""
        g = self._client(
            monkeypatch, 200,
            {"data": {"translations": [{"translatedText": "配送途中"}]}},
        )
        assert g.translate("配送途中") == "配送途中"


class TestListingCacheInvalidation:
    """改了标题必须清掉榜单/厂牌的列表快照。

    那两份缓存存的是 enrich_codes 的完整结果（整行详情，cn_title 也在里面），
    TTL 30 / 60 分钟。不清的话库里已经是中文、列表页却还在发旧 JSON ——
    表现就是「点完翻译卡片变中文，一刷新又变回日文」。
    """

    def _seed_with_cache(self, code="LC-001", cn=""):
        import json
        from app import services
        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            session.merge(Code(code=code, title=JA_TITLE, cn_title=cn))
        # 模拟列表页访问过一次，快照里是翻译前的样子
        services.set_rank_cache(
            "rank", "daily", json.dumps([{"code": code, "cn_title": cn}])
        )
        services.set_rank_cache(
            "brand", "prestige", json.dumps([{"code": code, "cn_title": cn}])
        )

    def test_manual_translate_drops_snapshot(self, monkeypatch):
        from app import services

        self._seed_with_cache()
        monkeypatch.setattr(services.translate, "is_available", lambda: True)
        monkeypatch.setattr(services, "translate_title", lambda t: "新译文")

        assert services.translate_code_title("LC-001").get("error") is None
        # 两个 namespace 都得清，否则厂牌页仍旧
        assert services.get_rank_cache("rank", "daily", ttl=1800) is None
        assert services.get_rank_cache("brand", "prestige", ttl=3600) is None

    def test_batch_translate_drops_snapshot(self, monkeypatch):
        from app import services

        self._seed_with_cache(code="LC-002")
        monkeypatch.setattr(services.translate, "is_available", lambda: True)
        monkeypatch.setattr(services, "translate_title", lambda t: "批量译文")
        monkeypatch.setattr(services, "TRANSLATE_WORKERS", 1)

        assert services.translate_codes(limit=10) >= 1
        assert services.get_rank_cache("rank", "daily", ttl=1800) is None

    def test_purge_drops_snapshot(self):
        """清掉存量拒绝说明后，列表快照同样得失效。"""
        from app import services

        self._seed_with_cache(code="LC-003", cn=REFUSAL)
        assert services.purge_refused_translations() >= 1
        assert services.get_rank_cache("rank", "daily", ttl=1800) is None

    def test_no_translation_leaves_cache_alone(self, monkeypatch):
        """翻译失败又没有旧拒绝说明可清时，别白清缓存。"""
        from app import services

        self._seed_with_cache(code="LC-004", cn="原有正常译文")
        monkeypatch.setattr(services.translate, "is_available", lambda: True)
        monkeypatch.setattr(services, "translate_title", lambda t: "")

        assert services.translate_code_title("LC-004").get("error")
        # 什么都没改，快照该留着 —— 否则每次失败都要让榜单重抓一遍
        assert services.get_rank_cache("rank", "daily", ttl=1800) is not None
