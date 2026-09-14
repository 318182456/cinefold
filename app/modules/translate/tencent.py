"""腾讯云机器翻译（TMT）。

放进降级链里作为百度/Google 之外的又一档。三家的取舍：

    百度    免费额度够用，但日译中的质量一般
    Google  质量最好，可惜没有免费额度，必须绑结算账号
    腾讯    每月 500 万字符免费，日译中质量介于两者之间

腾讯这档对本项目特别合适：片名是日文，而腾讯的日中翻译不像通用 AI 那样
对露骨内容做道德判断 —— 它是翻译 API，照翻不误。AI 那档被网关拦下来时
（见 translateai 里那串 REFUSAL 注释），这里往往能正常出结果。

签名走 TC3-HMAC-SHA256，只用标准库的 hmac/hashlib 手写，不引腾讯 SDK：
SDK 会拖进一串依赖，而这里只调一个接口，签名逻辑也就三十来行。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import httpx
from loguru import logger

from app.core.config import get_settings

HOST = "tmt.tencentcloudapi.com"
API_URL = f"https://{HOST}"
SERVICE = "tmt"
ACTION = "TextTranslate"
VERSION = "2018-03-21"

# 腾讯的语言代码：日文是 ja，中文是 zh。与百度的 jp/zh 不同，别抄错
DEFAULT_FROM = "ja"
DEFAULT_TO = "zh"

# 这些错误码重试也没用，是账号/配置的问题，值得单独说清楚。
# 其余错误码按普通失败处理，让工厂降级到下一家
_FATAL_CODES = {
    "AuthFailure.SignatureFailure": "签名校验失败，检查 TENCENT_SECRET_KEY 是否填对",
    "AuthFailure.SecretIdNotFound": "SecretId 不存在，检查 TENCENT_SECRET_ID",
    "AuthFailure.TokenFailure": "临时密钥已失效",
    "UnauthorizedOperation.CamNoAuth": "该账号未授权机器翻译，需在访问管理里加 QcloudTMTFullAccess",
    "FailedOperation.NoFreeAmount": "本月免费额度已用完，需在腾讯云开通后付费",
    "FailedOperation.ServiceIsolate": "账户欠费，翻译服务已隔离",
}


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


class Tencent:
    # 账号级的错误（欠费、没授权）每条番号都会撞一次，只打一遍就够了
    _fatal_warned: set[str] = set()

    def __init__(
        self, secret_id: str = "", secret_key: str = "", region: str = ""
    ):
        settings = get_settings()
        self.secret_id = secret_id or settings.tencent_secret_id
        self.secret_key = secret_key or settings.tencent_secret_key
        self.region = region or settings.tencent_region or "ap-guangzhou"
        self.proxy = settings.proxy or None

    @property
    def enabled(self) -> bool:
        return bool(self.secret_id and self.secret_key)

    def _authorization(self, payload: str, timestamp: int) -> str:
        """按 TC3-HMAC-SHA256 算出 Authorization 头。

        腾讯这套签名对格式极其挑剔：规范请求串里的 header 必须小写、按
        字典序排、且结尾要留一个换行；少一个换行就是 SignatureFailure，
        而错误信息不会告诉你差在哪。所以下面每一步都照文档的顺序写死。
        """
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")

        # 1) 规范请求串
        canonical_headers = (
            f"content-type:application/json; charset=utf-8\n"
            f"host:{HOST}\n"
            f"x-tc-action:{ACTION.lower()}\n"
        )
        signed_headers = "content-type;host;x-tc-action"
        hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        canonical_request = (
            f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"
        )

        # 2) 待签字符串
        credential_scope = f"{date}/{SERVICE}/tc3_request"
        hashed_request = hashlib.sha256(
            canonical_request.encode("utf-8")
        ).hexdigest()
        string_to_sign = (
            f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n{hashed_request}"
        )

        # 3) 逐层派生签名密钥
        secret_date = _sign(f"TC3{self.secret_key}".encode("utf-8"), date)
        secret_service = _sign(secret_date, SERVICE)
        secret_signing = _sign(secret_service, "tc3_request")
        signature = hmac.new(
            secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        return (
            f"TC3-HMAC-SHA256 Credential={self.secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

    def translate(
        self, text: str, from_lang: str = DEFAULT_FROM, to_lang: str = DEFAULT_TO
    ) -> str:
        if not self.enabled or not text:
            return ""

        # ProjectId 必填，0 是默认项目
        payload = json.dumps(
            {
                "SourceText": text,
                "Source": from_lang,
                "Target": to_lang,
                "ProjectId": 0,
            },
            ensure_ascii=False,
        )
        timestamp = int(time.time())

        try:
            headers = {
                "Authorization": self._authorization(payload, timestamp),
                "Content-Type": "application/json; charset=utf-8",
                "Host": HOST,
                "X-TC-Action": ACTION,
                "X-TC-Version": VERSION,
                "X-TC-Timestamp": str(timestamp),
                "X-TC-Region": self.region,
            }
            # trust_env=False 的理由同其它几个模块：系统代理会把这类请求
            # 吞掉。要走代理就显式配 PROXY
            with httpx.Client(timeout=20, proxy=self.proxy, trust_env=False) as client:
                response = client.post(
                    API_URL, headers=headers, content=payload.encode("utf-8")
                )
                response.raise_for_status()
                body = response.json().get("Response") or {}

            error = body.get("Error")
            if error:
                code = error.get("Code", "")
                hint = _FATAL_CODES.get(code)
                if hint:
                    # 账号级问题，每条番号都会撞，只提示一次
                    if code not in Tencent._fatal_warned:
                        Tencent._fatal_warned.add(code)
                        logger.warning(f"腾讯翻译不可用（{code}）：{hint}。本次运行不再重复提示")
                else:
                    logger.warning(
                        f"腾讯翻译失败 {code}: {error.get('Message', '')}"
                    )
                return ""

            return (body.get("TargetText") or "").strip()
        except Exception as exc:
            logger.warning(f"腾讯翻译异常: {exc}")
            return ""
