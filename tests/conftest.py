"""测试环境隔离。

必须在导入 app 之前设置：配置层会在 import 时确定路径，
若不隔离，测试会读到项目根目录的 app.env 并真的去连外部服务。
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="cinefold-tests-"))

os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["CONFIG_DIR"] = str(_TMP / "config")
os.environ["LOG_DIR"] = str(_TMP / "logs")
os.environ["ENVIRONMENT"] = "test"

# 指向空目录，屏蔽项目根目录下的 app.env
os.environ["CINEFOLD_DISABLE_FALLBACK_ENV"] = "1"

for path in (_TMP / "data", _TMP / "config", _TMP / "logs"):
    path.mkdir(parents=True, exist_ok=True)

# 清掉可能从外部环境继承的真实凭证
for key in (
    "QBITTORRENT_URL", "QBITTORRENT_USERNAME", "QBITTORRENT_PASSWORD",
    "TRANSMISSION_URL", "TRANSMISSION_USERNAME", "TRANSMISSION_PASSWORD",
    "EMBY_URL", "EMBY_API_KEY", "JELLYFIN_URL", "JELLYFIN_API_KEY",
    "PLEX_URL", "PLEX_TOKEN", "MTEAM_API_KEY",
    "ROUSI_TOKEN", "ROUSI_PASSKEY", "ROUSI_HOST",
    "PTT_COOKIE", "PTT_HOST", "NICEPT_COOKIE", "NICEPT_HOST",
    "TELEGRAM_BOT_TOKEN", "WECHAT_CORP_ID", "BT_URL",
    # AI 助手会在自身配置留空时回退到 OPENAI_*，两组都得清，
    # 否则 agent 测试会真的去请求接口
    "OPENAI_API_KEY", "OPENAI_URL", "OPENAI_MODEL",
    "AGENT_URL", "AGENT_MODEL", "AGENT_API_KEY",
    "BAIDU_API_KEY", "GOOGLE_API_KEY",
    # 留着会让测试连到真实的数据库与 Redis
    "DATABASE_URL", "REDIS_URL", "REDIS_JOB_STORE",
    # 留着会让测试真的去调过盾服务：单次过盾几十秒，还受进程级锁串行，
    # 整套测试会被拖成十几分钟
    "BYPASS_URL", "FLARE_SOLVERR_URL",
    # 代理同理，会把本该失败的直连绕出去，测试结果随外部网络漂移
    "PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
):
    os.environ.pop(key, None)
