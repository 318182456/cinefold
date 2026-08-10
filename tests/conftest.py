"""测试环境隔离。

必须在导入 app 之前设置：配置层会在 import 时确定路径，
若不隔离，测试会读到项目根目录的 app.env 并真的去连外部服务。
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="bytemuse-tests-"))

os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["CONFIG_DIR"] = str(_TMP / "config")
os.environ["LOG_DIR"] = str(_TMP / "logs")
os.environ["ENVIRONMENT"] = "test"

# 指向空目录，屏蔽项目根目录下的 app.env
os.environ["BYTE_MUSE_DISABLE_FALLBACK_ENV"] = "1"

for path in (_TMP / "data", _TMP / "config", _TMP / "logs"):
    path.mkdir(parents=True, exist_ok=True)

# 清掉可能从外部环境继承的真实凭证
for key in (
    "QBITTORRENT_URL", "QBITTORRENT_USERNAME", "QBITTORRENT_PASSWORD",
    "TRANSMISSION_URL", "TRANSMISSION_USERNAME", "TRANSMISSION_PASSWORD",
    "EMBY_URL", "EMBY_API_KEY", "JELLYFIN_URL", "JELLYFIN_API_KEY",
    "PLEX_URL", "PLEX_TOKEN", "MTEAM_API_KEY",
    "ROUSI_COOKIE", "PTT_COOKIE", "NICEPT_COOKIE",
    "TELEGRAM_BOT_TOKEN", "WECHAT_CORP_ID", "BT_URL",
    "OPENAI_API_KEY", "BAIDU_API_KEY", "GOOGLE_API_KEY",
):
    os.environ.pop(key, None)
