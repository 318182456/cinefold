"""手动翻译单个番号。

定时任务只翻 cn_title 为空的，译文落库后就永不重来 —— 机翻把标题译坏时
用户得有个办法当场要求重译，这组用例盯的就是那条路径。
"""

import pytest


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

    # 下面这组是 2026-09-14 线上误杀的真实样本。原先用关键词表判拒绝
    # （含「违反」「抱歉」），而这些词在片名里本来就合法且高频 ——
    # 「校則違反」是 JAV 的常见题材词，一整类片子的译文全被毙掉。
    #
    # 代价不止丢译文：丢了算一次失败，攒够 TRANSLATE_FAILURE_LIMIT 就
    # 触发 give_up 跳过本轮剩余番号；purge_refused_translations 每轮还
    # 拿同一个判断扫全库，就算侥幸存进去也会被清空，永远修不好。
    #
    # 所以这组用例守的是「别再退回按词匹配」。
    @pytest.mark.parametrize(
        "source, translated",
        [
            (
                "校則違反の連帯責任！男子がわざと校則違反、罰として男女とも"
                "下半身丸出し半裸で登校！私が通う超進学校の校則は厳しすぎる。",
                "违反校规连带责任！男生故意犯规，作为惩罚男女生下半身全裸半裸"
                "上学！我就读的超级升学名校校规极其严苛。",
            ),
            (
                "【4K】校則違反ブルマ女子生徒と禁断の中出し性交 宮西光",
                "【4K】违反校规体操服学生与禁断的内射性交 宫西光",
            ),
            (
                "【4K】校則違反スク水女子生徒と禁断の中出し性交 真白ふわり",
                "【4K】与违反校规泳装的学生进行禁断的中出性交 真白芙羽",
            ),
            (
                "校則違反スク水を着た彼女に誘惑されて、何度も中出しセックス"
                "……THE BEST 8時間",
                "被穿着违反校规泳装的她诱惑，一次又一次地内射做爱……"
                "THE BEST 8小时",
            ),
            (
                "【4K】学校一の美少女で敏感体質な女子生徒に校則違反の水着で"
                "誘惑されて…果林と何度も中出しセックス 北岡果林",
                "【4K】被学校第一美少女、拥有敏感身体的女学生穿着违反校规的"
                "泳衣诱惑我……与果林一次又一次地内射做爱 北冈果林",
            ),
            # 「抱歉」出现在片名中段，不是开头的道歉话术
            (
                "本物降臨★即凍結の商品★奇跡の復活★高額深謝★★"
                "最近の子の発育が凄すぎる★",
                "真实出现★立即冻结的商品★奇迹复活★高价深感抱歉★★"
                "最近孩子的发育太惊人了★",
            ),
        ],
    )
    def test_titles_containing_refusal_words_survive(self, source, translated):
        """片名里出现「违反」「抱歉」不等于这是一句拒绝。"""
        from app.modules.translate.translateai import looks_like_refusal

        assert looks_like_refusal(translated, source) is False

    @pytest.mark.parametrize(
        "refusal",
        [
            "抱歉，我无法翻译这个内容。",
            "很抱歉，我不能提供该内容的翻译。",
            "我无法为您提供翻译服务。",
            "无法翻译该内容，因为它违反了内容政策。",
            "该请求已被拦截。",
            "I cannot translate this content.",
            "I'm sorry, but I can't assist with that.",
            "Sorry, I am unable to provide a translation for this.",
            "As an AI language model, I cannot help with this request.",
            "Unfortunately, this request violates our content policy.",
        ],
    )
    def test_refusal_phrasings_still_caught(self, refusal):
        """收紧误杀之后，真正的拒绝话术一条都不能放过去。"""
        from app.modules.translate.translateai import looks_like_refusal

        assert looks_like_refusal(refusal, JA_TITLE) is True

    def test_bare_refusal_verbs_are_not_enough(self):
        """「无法」「不能」单独出现一律放行 —— 它们是常见的片名用词。

        判据改成整句话术之后，这里不再看「动词+宾语」的搭配（那一版
        把「无法满足」误杀了），只认完整短语。
        """
        from app.modules.translate.translateai import looks_like_refusal

        for good, src in [
            ("无法忍受的痴汉电车", "我慢できない痴漢電車"),
            ("不能说的秘密", "言えない秘密"),
            ("无法满足的人妻", "満たされない人妻"),
            ("我无法停止高潮", "イキが止まらない"),
        ]:
            assert looks_like_refusal(good, src) is False, good

        # 完整话术才判拒绝
        assert looks_like_refusal("我无法翻译这个标题。", JA_TITLE) is True

    def test_long_title_with_refusal_words_survives(self):
        """长片名里凑出「无法…」不算拒绝。

        2026-09-14 的第二批误杀：「仅靠与丈夫做爱无法满足的欲求不满人妻」
        —— 欲求不满是常见题材，「无法满足」是片名的一部分。拒绝说明都是
        短句，长文本交给长度比那关判，别在搭配关上误杀。
        """
        from app.modules.translate.translateai import looks_like_refusal

        title = (
            "搭讪人妻 精液中出 16人 5小时 仅靠与丈夫做爱无法满足的欲求不满人妻 "
            "走上街头被搭讪 享受萍水相逢的性爱 用浓厚精子灌满沾满淫液的骚穴的色情人妻"
        )
        source = (
            "ナンパ人妻 精液中出し 16人 5時間 夫とのセックスだけでは満たされない"
            "欲求不満妻 街に出てナンパされ 行きずりのセックスを楽しむ"
        )
        assert looks_like_refusal(title, source) is False

    # 2026-09-14 的第三批误杀：「对不起」开头的片名。「ごめんなさい…」是
    # JAV 的高频标题句式，而原先把道歉词本身当成拒绝信号。真正的拒绝是
    # 「道歉 + 拒绝动作」，光道歉的那是片名。
    @pytest.mark.parametrize(
        "translated",
        [
            "对不起，我是个好色的女人…被看穿情欲而堕落的肉体／高桥结香",
            "对不起……。大白天就裸求索取",
            "对不起…我忍不住在里面高潮了【可爱爆发中偶像脸两人】",
            "对不起，亲爱的… 持临时驾照的巨乳新婚妻子瞒着丈夫与大屌教练堕入不伦 与田铃",
            "对不起。我已经只能对年上男人那种缠绵执着的SEX有感觉了。 叶山さゆり",
            "对不起，我要高潮了！优雅敏感的人妻下半身却很淫荡。恳求直接插入，就这样内射！",
            "对不起男友！我想让你看着我和别的男人做！！ - 紺野咲",
            "对不起 漏尿美容院 用禁欲使少女身体隐隐作痛的慢慢逼疯失禁油压按摩 根尾明里",
            "抱歉我来晚了 邻居人妻的诱惑",
        ],
    )
    def test_apology_opening_titles_survive(self, translated):
        """光是道歉不算拒绝 —— 后面得跟着「无法/不能」才算。"""
        from app.modules.translate.translateai import looks_like_refusal

        assert looks_like_refusal(translated, "ごめんなさい…" * 6) is False

    @pytest.mark.parametrize(
        "refusal",
        [
            "对不起，我无法翻译这个内容。",
            "抱歉，我不能提供该内容的翻译。",
            "很抱歉，由于内容限制我不能协助。",
            "非常抱歉，我没办法处理这个请求。",
            "I'm sorry, but I can't assist with that.",
            "Sorry, I am unable to provide a translation.",
        ],
    )
    def test_apology_plus_refusal_still_caught(self, refusal):
        """道歉后面跟上拒绝动作，仍然要判成拒绝。"""
        from app.modules.translate.translateai import looks_like_refusal

        assert looks_like_refusal(refusal, JA_TITLE) is True

    def test_thinking_trace_is_rejected(self):
        """推理模型把自言自语吐出来了，那不是译文。

        实测样本里夹着中文碎片，乍看还挺像回事 —— 比拒绝说明更难发现，
        因为拒绝至少一眼看得出不对。
        """
        from app.modules.translate.translateai import looks_like_refusal

        trace = (
            "步骤\n"
            "Wait, let me reconsider. あゆみ is a Japanese name.\n"
            "步\n"
            "Actually, あゆみ as a name"
        )
        assert looks_like_refusal(trace, "あゆみの日本語タイトル") is True

    def test_thinking_markers_dont_hit_normal_titles(self):
        """元话语标记别误伤正常译文。"""
        from app.modules.translate.translateai import looks_like_refusal

        for good, src in [
            ("我实际上很喜欢这样", "実は好き"),
            ("步的女人", "歩く女"),
            ("等一下，别停下来", "待って、止めないで"),
        ]:
            assert looks_like_refusal(good, src) is False, good

    def test_refusal_object_list_stays_narrow(self):
        """「无法满足」「无法处理」「无法完成」都是正常片名用词。"""
        from app.modules.translate.translateai import looks_like_refusal

        for good, src in [
            ("无法满足的人妻", "満たされない人妻"),
            ("无法处理的巨乳", "持て余す巨乳"),
            ("无法完成的任务", "終わらない任務"),
            ("无法停止的痉挛绝顶", "止まらない痙攣絶頂"),
        ]:
            assert looks_like_refusal(good, src) is False, good


class TestServiceUnavailable:
    """网关 5xx / 超时要和「这条内容被拒」分开。

    两者在上层的处置相反：服务挂了该立刻停整轮（实测 503 时一口气打了
    13 次），内容被拒只该跳过这一条。原先都返回空串，上层区分不了。
    """

    def _client(self):
        from app.modules.translate.translateai import TranslateAI

        return TranslateAI(url="http://x/v1", model="m", api_key="k")

    @pytest.mark.parametrize("status", [500, 502, 503, 429])
    def test_server_errors_raise_unavailable(self, status, monkeypatch):
        import httpx

        from app.modules.translate.translateai import TranslateUnavailable

        def boom(*a, **kw):
            req = httpx.Request("POST", "http://x/v1/chat/completions")
            resp = httpx.Response(status, request=req)
            raise httpx.HTTPStatusError(str(status), request=req, response=resp)

        monkeypatch.setattr(httpx.Client, "post", boom)
        with pytest.raises(TranslateUnavailable):
            self._client().translate("テスト")

    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_client_errors_return_empty(self, status, monkeypatch):
        """4xx 多半是请求本身不对，算这一条的失败，不是服务挂了。"""
        import httpx

        def boom(*a, **kw):
            req = httpx.Request("POST", "http://x/v1/chat/completions")
            resp = httpx.Response(status, request=req)
            raise httpx.HTTPStatusError(str(status), request=req, response=resp)

        monkeypatch.setattr(httpx.Client, "post", boom)
        assert self._client().translate("テスト") == ""

    def test_timeout_raises_unavailable(self, monkeypatch):
        import httpx

        from app.modules.translate.translateai import TranslateUnavailable

        def boom(*a, **kw):
            raise httpx.TimeoutException("timeout")

        monkeypatch.setattr(httpx.Client, "post", boom)
        with pytest.raises(TranslateUnavailable):
            self._client().translate("テスト")

    def test_factory_falls_back_when_one_is_down(self, monkeypatch):
        """某一家挂了不代表下一家也挂，照常降级。"""
        from app.modules import translate as factory
        from app.modules.translate.translateai import TranslateUnavailable

        class Down:
            def translate(self, t):
                raise TranslateUnavailable("挂了")

        class Up:
            def translate(self, t):
                return "译文"

        monkeypatch.setattr(factory, "get_translators", lambda: [Down(), Up()])
        assert factory.translate("テスト") == "译文"

    def test_factory_raises_when_all_are_down(self):
        """每一家都不可用才算整条链路不通。"""
        from app.modules import translate as factory
        from app.modules.translate.translateai import TranslateUnavailable

        class Down:
            def translate(self, t):
                raise TranslateUnavailable("挂了")

        import unittest.mock as mock

        with mock.patch.object(factory, "get_translators", lambda: [Down(), Down()]):
            with pytest.raises(TranslateUnavailable):
                factory.translate("テスト")

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


class TestListingCachePatch:
    """改了标题要把榜单/厂牌快照里那一条同步掉，而且是就地替换不是整份丢弃。

    那两份缓存存的是 enrich_codes 的完整结果（整行详情，cn_title 也在里面），
    TTL 30 / 60 分钟。不同步的话库里已经是中文、列表接口却还在发旧 JSON ——
    表现就是「点完翻译卡片变中文，一刷新又变回日文」。

    但也不能整份清掉：重建榜单要把缺详情的番号逐个跨境重抓（实测单个番号
    11 个源全试 68 秒），点一次翻译就让整页卡住。
    """

    def _seed_with_cache(self, code="LC-001", cn="", other="OTHER-999"):
        import json
        from app import services
        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            session.merge(Code(code=code, title=JA_TITLE, cn_title=cn))
        # 快照里除了目标番号还有别人，用来确认没被连带影响
        snapshot = json.dumps([
            {"code": code, "cn_title": cn, "title": JA_TITLE, "star": "4.2"},
            {"code": other, "cn_title": "别人的译文", "title": "他人のタイトル"},
        ])
        services.set_rank_cache("rank", "daily", snapshot)
        services.set_rank_cache("brand", "prestige:7:14", snapshot)

    def _snapshot(self, ns="rank", key="daily", ttl=1800):
        import json
        from app import services

        raw = services.get_rank_cache(ns, key, ttl=ttl)
        return json.loads(raw) if raw else None

    def test_manual_translate_patches_in_place(self, monkeypatch):
        """快照要留着，只有那一条的 cn_title 变了。"""
        from app import services

        self._seed_with_cache()
        monkeypatch.setattr(services.translate, "is_available", lambda: True)
        monkeypatch.setattr(services, "translate_title", lambda t: "新译文")

        assert services.translate_code_title("LC-001").get("error") is None

        for ns, key, ttl in (("rank", "daily", 1800), ("brand", "prestige:7:14", 3600)):
            items = self._snapshot(ns, key, ttl)
            # 整份还在（没被丢弃），否则榜单要重抓
            assert items is not None, f"{ns} 快照被整份清掉了"
            assert len(items) == 2
            target = next(i for i in items if i["code"] == "LC-001")
            assert target["cn_title"] == "新译文"
            # 同一条里的其他字段不能丢
            assert target["star"] == "4.2"
            assert target["title"] == JA_TITLE
            # 别的番号不受影响
            other = next(i for i in items if i["code"] == "OTHER-999")
            assert other["cn_title"] == "别人的译文"

    def test_batch_translate_patches_each(self, monkeypatch):
        from app import services
        from app.database.models import Code
        from app.database.session import session_scope

        self._seed_with_cache(code="LC-002")
        # 别的用例会在同一个库里留下待翻译的行，limit 有可能全被它们占满，
        # 轮不到 LC-002。先清干净，让这条用例只面对自己造的数据
        with session_scope() as session:
            from sqlalchemy import select as _select

            for row in session.scalars(
                _select(Code).where(Code.code != "LC-002")
            ).all():
                session.delete(row)

        monkeypatch.setattr(services.translate, "is_available", lambda: True)
        monkeypatch.setattr(services, "translate_title", lambda t: "批量译文")
        monkeypatch.setattr(services, "TRANSLATE_WORKERS", 1)

        assert services.translate_codes(limit=10) >= 1
        items = self._snapshot()
        assert items is not None
        assert next(i for i in items if i["code"] == "LC-002")["cn_title"] == "批量译文"

    def test_purge_patches_snapshot(self):
        """清掉存量拒绝说明后，快照里那句英文也要抹掉。"""
        from app import services

        self._seed_with_cache(code="LC-003", cn=REFUSAL)
        assert services.purge_refused_translations() >= 1
        items = self._snapshot()
        assert items is not None
        assert next(i for i in items if i["code"] == "LC-003")["cn_title"] == ""

    def test_no_translation_leaves_snapshot_untouched(self, monkeypatch):
        """翻译失败又没有旧拒绝说明可清时，快照一个字都不该动。"""
        from app import services

        self._seed_with_cache(code="LC-004", cn="原有正常译文")
        monkeypatch.setattr(services.translate, "is_available", lambda: True)
        monkeypatch.setattr(services, "translate_title", lambda t: "")

        assert services.translate_code_title("LC-004").get("error")
        items = self._snapshot()
        assert items is not None
        assert next(i for i in items if i["code"] == "LC-004")["cn_title"] == "原有正常译文"

    def test_patch_ignores_code_absent_from_snapshot(self):
        """番号不在快照里就什么都不改，别把无关 key 写一遍。"""
        from app import services

        self._seed_with_cache(code="LC-005")
        before = services.get_rank_cache("rank", "daily", ttl=1800)
        assert services.patch_listing_cache("NOT-INSNAPSHOT") == 0
        assert services.get_rank_cache("rank", "daily", ttl=1800) == before

    def test_ttl_not_extended_by_patch(self):
        """替换不能顺手给快照续命，否则榜单长期拿不到新数据。"""
        from app import services
        from app.database.models import Cache
        from app.database.session import session_scope
        from sqlalchemy import select as _select

        self._seed_with_cache(code="LC-006")
        with session_scope() as session:
            row = session.scalar(
                _select(Cache).where(Cache.namespace == "rank", Cache.key == "daily")
            )
            before = row.create_time if row else None

        services.patch_listing_cache("LC-006", cn_title="改过了")

        with session_scope() as session:
            row = session.scalar(
                _select(Cache).where(Cache.namespace == "rank", Cache.key == "daily")
            )
            assert row is not None
            # create_time 是 get_rank_cache 判 TTL 的依据，不能被刷新
            assert row.create_time == before


class TestPurgeCircuitBreaker:
    """存量清洗要有上限。

    purge_refused_translations 每轮翻译前扫全库，判断逻辑但凡有偏差就会
    批量误清 —— 2026-09-14 线上一轮清掉了 92 条正常译文，而日志只有一行
    INFO。真正的拒绝说明是少数，占比高必然意味着判断出了问题。
    """

    def _seed(self, good: int, bad: int):
        from app.database.base import DBBase
        from app.database.models import Code
        from app.database.session import engine, session_scope

        DBBase.metadata.create_all(engine)
        with session_scope() as session:
            session.query(Code).delete()
            for i in range(good):
                session.add(
                    Code(code=f"PG-{i:03d}", title="日文タイトル", cn_title="正常译文")
                )
            for i in range(bad):
                session.add(
                    Code(code=f"PB-{i:03d}", title="日文タイトル", cn_title="抱歉，我无法翻译")
                )

    def _empty_count(self) -> int:
        from app.database.models import Code
        from app.database.session import session_scope

        with session_scope() as session:
            return (
                session.query(Code)
                .filter((Code.cn_title == "") | (Code.cn_title.is_(None)))
                .count()
            )

    def test_small_batch_is_cleared(self):
        """少量拒绝说明照常清掉 —— 熔断不该把正常功能也挡住。"""
        from app import services

        self._seed(good=100, bad=5)
        assert services.purge_refused_translations() == 5

    def test_oversized_batch_clears_nothing(self, monkeypatch):
        """判断逻辑坏掉时，一条都不清，库必须原样留着。"""
        from app import services
        from app.modules.translate import translateai

        self._seed(good=100, bad=0)
        # 模拟判断逻辑出错：所有译文都被判成拒绝
        monkeypatch.setattr(translateai, "looks_like_refusal", lambda t, s="": True)

        assert services.purge_refused_translations() == 0
        # 关键断言：库没被洗
        assert self._empty_count() == 0

    def test_absolute_floor_protects_small_libraries(self):
        """小库里比例天然偏高，绝对下限兜住，别让它清不动。"""
        from app import services

        self._seed(good=2, bad=1)
        assert services.purge_refused_translations() == 1


class TestTencentTranslate:
    """腾讯云机器翻译。

    降级链里 AI 之后的主力：片名普遍露骨，AI 网关常以内容为由拦下，而
    腾讯是翻译 API 不作道德判断，照翻不误。

    签名走 TC3-HMAC-SHA256 手写（不引 SDK），格式极其挑剔 —— 规范请求串
    少一个换行就是 SignatureFailure，且错误信息不会告诉你差在哪。下面这条
    签名用例的期望值是拿腾讯官方 SDK 交叉验证出来的，改签名逻辑时它会第一
    个报警。
    """

    def _client(self):
        from app.modules.translate.tencent import Tencent

        return Tencent(
            secret_id="AKIDzTESTSECRETIDEXAMPLE0000000000",
            secret_key="TestSecretKeyExample000000000000",
            region="ap-guangzhou",
        )

    def test_signature_matches_official_sdk(self):
        """签名必须与腾讯官方 SDK 算出的逐字节一致。

        期望值来自 tencentcloud-sdk-python-common 的 Sign.sign_tc3，
        用同样的密钥/时间戳/payload 跑出来的结果（SDK 只用于验证，
        没有进 requirements）。
        """
        import json

        body = json.dumps(
            {"SourceText": "テスト", "Source": "ja", "Target": "zh", "ProjectId": 0},
            ensure_ascii=False,
        )
        auth = self._client()._authorization(body, 1700000000)
        signature = auth.split("Signature=")[1]

        assert signature == (
            "d90dddb241dce44a4a14c31725b6c760555268f30a34f32903947eb17da6381f"
        )

    def test_authorization_header_shape(self):
        """Authorization 的结构也别改坏了。"""
        auth = self._client()._authorization("{}", 1700000000)

        assert auth.startswith("TC3-HMAC-SHA256 Credential=")
        assert "/2023-11-14/tmt/tc3_request" in auth
        assert "SignedHeaders=content-type;host;x-tc-action" in auth

    def test_disabled_without_credentials(self):
        """凭据不全就不启用，别发一个必然 401 的请求。"""
        from app.modules.translate.tencent import Tencent

        assert Tencent(secret_id="", secret_key="").enabled is False
        assert Tencent(secret_id="only-id", secret_key="").enabled is False
        assert Tencent(secret_id="i", secret_key="k").enabled is True

    def test_successful_translation(self, monkeypatch):
        import httpx

        def fake_post(self, url, **kw):
            return httpx.Response(
                200,
                json={"Response": {"TargetText": "测试", "RequestId": "r"}},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        assert self._client().translate("テスト") == "测试"

    def test_api_error_returns_empty(self, monkeypatch):
        """接口报错要返回空串，好让工厂降级到下一家。"""
        import httpx

        def fake_post(self, url, **kw):
            return httpx.Response(
                200,
                json={"Response": {"Error": {"Code": "InvalidParameter", "Message": "bad"}}},
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        assert self._client().translate("テスト") == ""

    def test_account_error_warns_once(self, monkeypatch, caplog):
        """欠费/未授权是账号级问题，每条番号都会撞，只提示一次。"""
        import httpx

        from app.modules.translate.tencent import Tencent

        Tencent._fatal_warned.clear()

        def fake_post(self, url, **kw):
            return httpx.Response(
                200,
                json={
                    "Response": {
                        "Error": {
                            "Code": "FailedOperation.NoFreeAmount",
                            "Message": "no free amount",
                        }
                    }
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.Client, "post", fake_post)
        client = self._client()
        for _ in range(5):
            assert client.translate("テスト") == ""

        assert "FailedOperation.NoFreeAmount" in Tencent._fatal_warned
        Tencent._fatal_warned.clear()

    def test_factory_order_puts_tencent_after_ai(self):
        """降级链顺序：AI → 腾讯 → 百度 → Google。"""
        from unittest import mock

        from app.core.config import Settings
        from app.modules import translate as factory

        settings = Settings(
            openai_url="http://x",
            openai_api_key="k",
            tencent_secret_id="i",
            tencent_secret_key="k",
            baidu_app_id="a",
            baidu_api_key="b",
            google_api_key="g",
        )
        with mock.patch.object(factory, "get_settings", lambda: settings):
            names = [t.__class__.__name__ for t in factory.get_translators()]

        assert names == ["TranslateAI", "Tencent", "Baidu", "Google"]


class TestGatewayTokenSignal:
    """网关拦截的确定性信号：usage.total_tokens == 0。

    这是主判据，looks_like_refusal 只是兜底。理由：片名可以是任何内容，
    按词猜「像不像拒绝」必然误杀（线上三批，每批都是正常译文被丢掉），
    而 token 消耗是客观的 —— 模型真跑了就必然烧 token，返回 0 说明请求
    在网关那层就被关键词扫描拦下了，回来的文字是网关自己写的。
    """

    def _client(self):
        from app.modules.translate.translateai import TranslateAI

        return TranslateAI(url="http://x/v1", model="m", api_key="k")

    def _respond(self, monkeypatch, content: str, usage: dict | None):
        import httpx

        body = {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        }
        if usage is not None:
            body["usage"] = usage

        def fake_post(self, url, **kw):
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.Client, "post", fake_post)

    def test_zero_tokens_is_treated_as_blocked(self, monkeypatch):
        """token 为 0 —— 哪怕内容看着像正常译文，也不能当译文收下。"""
        self._respond(monkeypatch, "违反校规的女学生", {"total_tokens": 0})
        assert self._client().translate("校則違反の女子生徒") == ""

    def test_normal_usage_passes_through(self, monkeypatch):
        """正常消耗了 token，就按译文收下。"""
        self._respond(monkeypatch, "违反校规的女学生", {"total_tokens": 42})
        assert self._client().translate("校則違反の女子生徒") == "违反校规的女学生"

    def test_missing_usage_does_not_block(self, monkeypatch):
        """有些网关正常响应也不回 usage。

        那种情况下「没有 usage」是「没报告」而不是「没消耗」，不能当成
        拦截 —— 否则接上这类网关会一条都翻不出来。
        """
        self._respond(monkeypatch, "违反校规的女学生", None)
        assert self._client().translate("校則違反の女子生徒") == "违反校规的女学生"

    def test_empty_content_still_empty(self, monkeypatch):
        """内容本来就是空的，不必扯到 token 上。"""
        self._respond(monkeypatch, "", {"total_tokens": 0})
        assert self._client().translate("校則違反の女子生徒") == ""


class TestStructuralRefusalHeuristics:
    """兜底判据只认「结构上不可能是片名」的形态，不按词猜。"""

    def test_english_prose_for_japanese_source(self):
        """原文是日文，回来一段英文散文 —— 那不是译文。"""
        from app.modules.translate.translateai import looks_like_refusal

        prose = (
            "This content appears to describe explicit material involving "
            "school settings which I would rather not render into Chinese."
        )
        assert looks_like_refusal(prose, "校則違反ブルマ女子生徒と禁断の中出し性交") is True

    # 判据必须是「净增」而不是「英文单词总数」。片名自带的英文标记在原文
    # 里同样存在，翻译时原样保留是正确行为 —— 按总数判会误杀，实测
    # 「【VR】庆祝 小熊猫VR 8周年…Happy Valentine's Day 特别BOX」有 9 个
    # 英文单词却净增 0。
    @pytest.mark.parametrize(
        "translated, source",
        [
            (
                "被穿着违反校规泳装的她诱惑……THE BEST 8小时",
                "校則違反スク水……THE BEST 8時間",
            ),
            ("【VR】【4K】我的女友 SEX 合集", "【VR】【4K】僕の彼女 SEX コレクション"),
            (
                "【VR】庆祝 小熊猫VR 8周年感谢！！SP Re:【一枚硬币】开始的情人节"
                "特别企划2nd 人气女优的Happy Valentine's Day 特别BOX 1",
                "【VR】祝 レッサーパンダVR 8周年感謝！！SP Re:【ワンコイン】から"
                "始まるバレンタイン特別企画2nd 人気女優のHappy Valentine's Day 特別BOX 1",
            ),
        ],
    )
    def test_english_markers_carried_over_are_fine(self, translated, source):
        """原文里就有的英文标记，译文保留不算问题。"""
        from app.modules.translate.translateai import looks_like_refusal

        assert looks_like_refusal(translated, source) is False

    def test_counts_only_newly_added_english(self):
        """直接验证净增的算法本身。"""
        from app.modules.translate.translateai import _extra_english_words

        # 原文里就有的，净增 0
        assert _extra_english_words(
            "【VR】全新篇章 THE BEST", "【VR】BLAND NEW CHAPTER THE BEST"
        ) == 0
        # 凭空多出来的才算
        assert _extra_english_words("这是 Simplified Chinese Translation", "これは") == 3

    def test_model_echoes_source_with_label(self):
        """模型把原文、标注、译文一起吐出来了。

        实测：
            【VR】BLAND NEW CHAPTER めるにゃん
            **Simplified Chinese Translation:**
            【VR】全新篇章 めるにゃん

        整段存进 cn_title 就是三行。净增英文只有 3 个（刚好不触发阈值），
        得靠标注话术认出来。
        """
        from app.modules.translate.translateai import looks_like_refusal

        dirty = (
            "【VR】BLAND NEW CHAPTER めるにゃん\n"
            "**Simplified Chinese Translation:**\n"
            "【VR】全新篇章 めるにゃん"
        )
        assert looks_like_refusal(dirty, "【VR】BLAND NEW CHAPTER めるにゃん") is True

    def test_english_source_is_exempt(self):
        """原文本来就是英文标题时，英文译文不该被这一关误判。"""
        from app.modules.translate.translateai import looks_like_refusal

        assert looks_like_refusal("The Best Collection 8 Hours", "The Best Collection 8 Hours") is False
