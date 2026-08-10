"""配置加载。

配置来源优先级：环境变量 > .env 文件 > 默认值。
首次启动时若 .env 不存在，会从 .env.example 复制一份。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/data/config"))
ENV_FILE = CONFIG_DIR / ".env"
ENV_EXAMPLE = Path(__file__).parent / ".env.example"

# 项目根目录下的 app.env 作为备用配置源，方便本地开发时直接编辑
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FALLBACK_ENV_FILES = (PROJECT_ROOT / "app.env", PROJECT_ROOT / ".env")

# 这些字段在返回给前端时会被打码
SENSITIVE_KEYS = {
    "emby_api_key", "plex_token", "jellyfin_api_key",
    "qbittorrent_password", "transmission_password", "thunder_authorization",
    "rousi_password", "rousi_token", "rousi_passkey",
    "ptt_cookie", "nicept_cookie",
    "mteam_api_key", "wechat_corp_secret", "wechat_token",
    "wechat_encoding_aes_key", "telegram_bot_token", "secret_key",
    "cloudnas_password", "baidu_api_key", "google_api_key", "openai_api_key",
    "github_token",
    # 连接串里通常内嵌账号密码
    "database_url", "redis_url",
}

DEFAULT_FILTER = {
    "only_chinese": False,
    "only_uc": False,
    "exclude_uc": False,
    "only_uhd": False,
    "only_free": False,
    "exclude_uhd": True,
    "include_keywords": "",
    "exclude_keywords": "",
    "min_size": "",
    "max_size": "",
}

DEFAULT_SORT = "free,chinese,uc,!uc,site,seeders,!uhd,uhd"


def _strip_quotes(value: str) -> str:
    """去掉配置值两端的引号。

    dotenv 会处理标准写法，但手工编辑的文件常出现 KEY='value' 这类残留。
    """
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _env(key: str, default: Any = "") -> str:
    return _strip_quotes(os.getenv(key, default)) or default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.getenv(key) or default)
    except ValueError:
        return default


def _env_json(key: str, default: Any) -> Any:
    raw = os.getenv(key, "").strip().strip("'\"")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


@dataclass
class Settings:
    # --- 媒体服务器 ---
    emby_url: str = ""
    emby_api_key: str = ""
    plex_url: str = ""
    plex_token: str = ""
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    jellyfin_user: str = ""

    # --- 下载器 ---
    qbittorrent_url: str = ""
    qbittorrent_username: str = ""
    qbittorrent_password: str = ""
    qbittorrent_download_path: str = ""
    qbittorrent_category: str = ""
    qbittorrent_verify_cert: bool = False
    transmission_url: str = ""
    transmission_username: str = ""
    transmission_password: str = ""
    transmission_download_path: str = ""
    transmission_label: str = ""
    transmission_verify_cert: bool = False
    thunder_url: str = ""
    thunder_file_id: str = ""
    thunder_authorization: str = ""

    # --- PT 站点 ---
    # Rousi 新站是前后端分离架构，用 Bearer token 而非 Cookie。
    # 填了用户名密码就会自动登录续期，token 可留空。
    rousi_username: str = ""
    rousi_password: str = ""
    rousi_token: str = ""
    rousi_passkey: str = ""
    ptt_cookie: str = ""
    nicept_cookie: str = ""
    mteam_api_key: str = ""

    # --- 企业微信 ---
    wechat_corp_id: str = ""
    wechat_corp_secret: str = ""
    wechat_agent_id: str = ""
    wechat_token: str = ""
    wechat_encoding_aes_key: str = ""
    wechat_to_user: str = "@all"
    wechat_proxy: str = ""
    wechat_photo: str = ""
    wechat_banner: bool = False

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_spoiler: bool = False
    telegram_whitelist: str = ""

    # --- 上行消息的番号订阅过滤 ---
    # 前缀名单，逗号分隔（如 NHDTB,SSIS）。白名单非空时只订阅名单内的前缀
    msg_allow_prefixes: str = ""
    msg_block_prefixes: str = ""
    # 单条消息最多订阅几个番号，0 表示不限
    msg_max_codes: int = 50
    # 订阅后立刻在后台检索 PT 并推送下载器，而不是等定时任务
    msg_auto_download: bool = False

    # --- 网络 ---
    proxy: str = ""
    external_domain: str = ""
    # 自建的反爬绕过服务地址（FlareSolverr 等），直连被拒时改走它
    bypass_url: str = ""
    # 图片代理的额外允许域名，逗号分隔；与内置白名单合并而非替换
    image_proxy_hosts: str = ""

    # --- 过滤排序 ---
    default_filter: dict = field(default_factory=lambda: dict(DEFAULT_FILTER))
    default_sort: str = DEFAULT_SORT
    max_actor: int = 3

    # --- 定时任务 ---
    sync_hot_time: str = "0 */2 * * *"
    sync_brands_time: str = "10 */2 * * *"
    sync_actors_time: str = "20 */2 * * *"
    fill_empty_image_time: str = "30 */12 * * *"
    sync_news: str = "45 */5 * * *"
    rank_schedule_time: str = "0 20 * * *"
    actor_schedule_time: str = "0 21 * * *"
    download_schedule_time: str = "0 22 * * *"

    # --- 榜单订阅 ---
    rank_page: int = 0
    rank_type: str = ""
    brand_type: str = ""
    main_site: str = "ALL"

    # --- 图片 ---
    image_mode: str = "BLUR"
    enable_photo_cache: bool = False
    enable_auto_complete: bool = True

    # --- 云盘 CD2 ---
    cloudnas_url: str = ""
    cloudnas_username: str = ""
    cloudnas_password: str = ""
    cloudnas_savepath: str = "/115open"

    # --- 翻译 ---
    baidu_api_key: str = ""
    baidu_app_id: str = ""
    google_api_key: str = ""
    openai_url: str = ""
    openai_model: str = ""
    openai_api_key: str = ""

    # --- 自定义 BT 源 ---
    bt_url: str = ""
    bt_json_data: str = ""
    bt_header: str = ""
    bt_method: str = "get"

    # --- 数据库 ---
    # 留空则使用 DATA_DIR 下的 SQLite 文件；填了就连 PostgreSQL 等外部库
    database_url: str = ""
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    # --- Redis ---
    # 留空则缓存回落到 cache 表，调度任务只存内存
    redis_url: str = ""
    redis_cache_ttl: int = 86400
    # 开启后 APScheduler 的任务状态持久化到 Redis，重启不丢
    redis_job_store: bool = False

    # --- 其他 ---
    javdb_host: str = "https://javdb.com"
    secret_key: str = ""
    # 检测新版本用。公开镜像可留空；私有镜像需要带 read:packages 权限的 token
    github_token: str = ""

    def to_safe_dict(self) -> dict:
        """返回给前端的配置，敏感字段打码。"""
        out = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in SENSITIVE_KEYS and value:
                out[f.name] = "*" * 8
            else:
                out[f.name] = value
        return out


def generate_secure_random_string(length: int = 48) -> str:
    return secrets.token_urlsafe(length)[:length]


def copy_env() -> None:
    """首次启动时初始化 .env。

    若项目根目录已有 app.env，则不生成空模板，避免覆盖用户的现成配置。
    """
    if ENV_FILE.exists():
        return
    if not os.getenv("BYTE_MUSE_DISABLE_FALLBACK_ENV") and any(
        f.exists() for f in FALLBACK_ENV_FILES
    ):
        return
    if ENV_EXAMPLE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(ENV_EXAMPLE, ENV_FILE)


def load_settings() -> Settings:
    copy_env()
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=True)
    elif not os.getenv("BYTE_MUSE_DISABLE_FALLBACK_ENV"):
        # 正式配置不存在时，回退到项目根目录的 app.env / .env
        for candidate in FALLBACK_ENV_FILES:
            if candidate.exists():
                load_dotenv(candidate, override=True)
                break

    s = Settings(
        emby_url=_env("EMBY_URL"),
        emby_api_key=_env("EMBY_API_KEY"),
        plex_url=_env("PLEX_URL"),
        plex_token=_env("PLEX_TOKEN"),
        jellyfin_url=_env("JELLYFIN_URL"),
        jellyfin_api_key=_env("JELLYFIN_API_KEY"),
        jellyfin_user=_env("JELLYFIN_USER"),

        qbittorrent_url=_env("QBITTORRENT_URL"),
        qbittorrent_username=_env("QBITTORRENT_USERNAME"),
        qbittorrent_password=_env("QBITTORRENT_PASSWORD"),
        qbittorrent_download_path=_env("QBITTORRENT_DOWNLOAD_PATH"),
        qbittorrent_category=_env("QBITTORRENT_CATEGORY"),
        qbittorrent_verify_cert=_env_bool("QBITTORRENT_VERIFY_CERT", False),
        transmission_url=_env("TRANSMISSION_URL"),
        transmission_username=_env("TRANSMISSION_USERNAME"),
        transmission_password=_env("TRANSMISSION_PASSWORD"),
        transmission_download_path=_env("TRANSMISSION_DOWNLOAD_PATH"),
        transmission_label=_env("TRANSMISSION_LABEL"),
        transmission_verify_cert=_env_bool("TRANSMISSION_VERIFY_CERT", False),
        thunder_url=_env("THUNDER_URL"),
        thunder_file_id=_env("THUNDER_FILE_ID"),
        thunder_authorization=_env("THUNDER_AUTHORIZATION"),

        rousi_username=_env("ROUSI_USERNAME"),
        rousi_password=_env("ROUSI_PASSWORD"),
        rousi_token=_env("ROUSI_TOKEN"),
        rousi_passkey=_env("ROUSI_PASSKEY"),
        ptt_cookie=_env("PTT_COOKIE"),
        nicept_cookie=_env("NICEPT_COOKIE"),
        mteam_api_key=_env("MTEAM_API_KEY"),

        wechat_corp_id=_env("WECHAT_CORP_ID"),
        wechat_corp_secret=_env("WECHAT_CORP_SECRET"),
        wechat_agent_id=_env("WECHAT_AGENT_ID"),
        wechat_token=_env("WECHAT_TOKEN"),
        wechat_encoding_aes_key=_env("WECHAT_ENCODING_AES_KEY"),
        wechat_to_user=_env("WECHAT_TO_USER", "@all"),
        wechat_proxy=_env("WECHAT_PROXY"),
        wechat_photo=_env("WECHAT_PHOTO"),
        wechat_banner=_env_bool("WECHAT_BANNER"),

        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        telegram_spoiler=_env_bool("TELEGRAM_SPOILER"),
        telegram_whitelist=_env("TELEGRAM_WHITELIST"),

        msg_allow_prefixes=_env("MSG_ALLOW_PREFIXES"),
        msg_block_prefixes=_env("MSG_BLOCK_PREFIXES"),
        msg_max_codes=_env_int("MSG_MAX_CODES", 50),
        msg_auto_download=_env_bool("MSG_AUTO_DOWNLOAD", False),

        proxy=_env("PROXY"),
        external_domain=_env("EXTERNAL_DOMAIN"),
        bypass_url=_env("BYPASS_URL") or _env("FLARE_SOLVERR_URL"),
        image_proxy_hosts=_env("IMAGE_PROXY_HOSTS"),

        default_filter=_env_json("DEFAULT_FILTER", dict(DEFAULT_FILTER)),
        default_sort=_env("DEFAULT_SORT", DEFAULT_SORT).strip("'\""),
        max_actor=_env_int("MAX_ACTOR", 3),

        sync_hot_time=_env("SYNC_HOT_TIME", "0 */2 * * *").strip("'\""),
        sync_brands_time=_env("SYNC_BRANDS_TIME", "10 */2 * * *").strip("'\""),
        sync_actors_time=_env("SYNC_ACTORS_TIME", "20 */2 * * *").strip("'\""),
        fill_empty_image_time=_env("FILL_EMPTY_IMAGE_TIME", "30 */12 * * *").strip("'\""),
        sync_news=_env("SYNC_NEWS", "45 */5 * * *").strip("'\""),
        rank_schedule_time=_env("RANK_SCHEDULE_TIME", "0 20 * * *").strip("'\""),
        actor_schedule_time=_env("ACTOR_SCHEDULE_TIME", "0 21 * * *").strip("'\""),
        download_schedule_time=_env("DOWNLOAD_SCHEDULE_TIME", "0 22 * * *").strip("'\""),

        rank_page=_env_int("RANK_PAGE", 0),
        rank_type=_env("RANK_TYPE"),
        brand_type=_env("BRAND_TYPE"),
        main_site=_env("MAIN_SITE", "ALL"),

        image_mode=_env("IMAGE_MODE", "BLUR"),
        enable_photo_cache=_env_bool("ENABLE_PHOTO_CACHE"),
        enable_auto_complete=_env_bool("ENABLE_AUTO_COMPLETE", True),

        cloudnas_url=_env("CLOUDNAS_URL"),
        cloudnas_username=_env("CLOUDNAS_USERNAME"),
        cloudnas_password=_env("CLOUDNAS_PASSWORD"),
        cloudnas_savepath=_env("CLOUDNAS_SAVEPATH", "/115open").strip("'\""),

        baidu_api_key=_env("BAIDU_API_KEY"),
        baidu_app_id=_env("BAIDU_APP_ID"),
        google_api_key=_env("GOOGLE_API_KEY"),
        openai_url=_env("OPENAI_URL"),
        openai_model=_env("OPENAI_MODEL"),
        openai_api_key=_env("OPENAI_API_KEY"),

        bt_url=_env("BT_URL"),
        bt_json_data=_env("BT_JSON_DATA"),
        bt_header=_env("BT_HEADER"),
        bt_method=_env("BT_METHOD", "get").strip("'\""),

        database_url=_env("DATABASE_URL"),
        db_pool_size=_env_int("DB_POOL_SIZE", 5),
        db_max_overflow=_env_int("DB_MAX_OVERFLOW", 10),
        db_echo=_env_bool("DB_ECHO", False),

        redis_url=_env("REDIS_URL"),
        redis_cache_ttl=_env_int("REDIS_CACHE_TTL", 86400),
        redis_job_store=_env_bool("REDIS_JOB_STORE", False),

        javdb_host=_env("JAVDB_HOST", "https://javdb.com"),
        secret_key=_env("SECRET_KEY"),
        github_token=_env("GITHUB_TOKEN"),
    )

    # SECRET_KEY 为空时生成并回写，保证重启后 JWT 仍然有效
    if not s.secret_key:
        s.secret_key = generate_secure_random_string()
        save_setting("SECRET_KEY", s.secret_key)

    return s


def save_setting(key: str, value: str) -> None:
    """写入单个配置项到 .env。"""
    save_settings({key: value})


def save_settings(updates: dict[str, Any]) -> None:
    """批量写入 .env，保留原有注释与顺序。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={_serialize(remaining.pop(key))}")
        else:
            out.append(line)

    for key, value in remaining.items():
        out.append(f"{key}={_serialize(value)}")

    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def _serialize(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (dict, list)):
        return f"'{json.dumps(value, ensure_ascii=False)}'"
    return str(value)


def is_production() -> bool:
    return os.getenv("ENVIRONMENT", "production").lower() == "production"


def is_development() -> bool:
    return not is_production()


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _settings
    if _settings is None or reload:
        _settings = load_settings()
    return _settings
