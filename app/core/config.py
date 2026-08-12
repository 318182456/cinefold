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
    "qbittorrent_password", "qbittorrent_apikey",
    "transmission_password", "thunder_authorization",
    "rousi_password", "rousi_token", "rousi_passkey",
    "ptt_cookie", "nicept_cookie",
    "mteam_api_key", "wechat_corp_secret", "wechat_token",
    "wechat_encoding_aes_key", "telegram_bot_token", "secret_key",
    "cloudnas_password", "baidu_api_key", "google_api_key", "openai_api_key",
    "github_token", "oidc_client_secret", "medialink_webhook_token",
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
    "only_vr": False,
    # VR 体积普遍很大且需专用播放器，默认排除
    "exclude_vr": True,
    "include_keywords": "",
    "exclude_keywords": "",
    "min_size": "",
    "max_size": "",
    # 做种数区间，0 表示不限
    "min_seeders": "",
    "max_seeders": "",
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

    # --- 媒体联动 ---
    # 媒体库根目录，刮削产物（硬链接）所在。按 inode 反查源文件时的扫描范围
    medialink_library_path: str = ""
    # 刮削工具实际写入硬链接的目录（如 <库根>/日本AV）。留空则等同于库根。
    # 与库根分开是因为库根常设成上一层，好把多个分类目录都纳入反查范围，
    # 但刮削产物只落在其中某一个子目录里 —— 这个目录受保护，不会被当成
    # 空目录清掉，也会在监控目录页列出来
    medialink_scrape_dir: str = ""
    # 媒体服务器删除影片时，同步删除种子与源文件。关闭时只记录不动手
    medialink_delete_enabled: bool = False
    # webhook 校验密钥。请求需带 X-Cinefold-Token，留空则不校验
    medialink_webhook_token: str = ""

    # --- 监控目录 ---
    # 文件消失后的删除宽限期（秒）。期间若同 inode 的文件在别处出现，判定为
    # 移动而非删除，只改记录不动文件。也能挡住网络存储瞬时不可达导致的误删。
    # 默认 30 分钟；设为 0 则发现即删
    watchdir_delete_grace: int = 1800
    # 自动同步总开关。关掉后定时对账与实时监听都不跑，只能手动点同步。
    # 想先观察一段时间再让它自动动手时关掉
    watchdir_auto_sync: bool = True
    # 定时全量对账的间隔（分钟）。NAS / Docker 绑定挂载上 inotify 事件常丢，
    # 这个兜底才是实际起作用的那条路径，间隔短一点响应更快。
    # 每轮要扫源目录并逐条比对，目录很大时别设太小
    watchdir_sync_interval: int = 30

    # --- 下载器 ---
    qbittorrent_url: str = ""
    qbittorrent_username: str = ""
    qbittorrent_password: str = ""
    # Web API Key（qb 5.2.0+ / WebAPI 2.14.1+）。配了就走 Bearer 鉴权，
    # 无需登录换 Cookie，账号密码可留空
    qbittorrent_apikey: str = ""
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
    # 上行消息的接收方式：webhook 需要公网 HTTPS，polling 靠长轮询主动取
    telegram_receive_mode: str = "webhook"

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
    # 主站优先，逗号分隔可给多个。排序规则含 site 时生效，未列出的站排在后面
    primary_site: str = ""
    max_actor: int = 3

    # --- 定时任务 ---
    sync_hot_time: str = "0 */2 * * *"
    sync_brands_time: str = "10 */2 * * *"
    sync_actors_time: str = "20 */2 * * *"
    fill_empty_image_time: str = "30 */12 * * *"
    sync_news: str = "45 */5 * * *"
    rank_schedule_time: str = "0 20 * * *"
    brand_schedule_time: str = "30 20 * * *"
    actor_schedule_time: str = "0 21 * * *"
    download_schedule_time: str = "0 22 * * *"
    # 榜单缓存 30 分钟过期，预热间隔必须比它短，否则用户还是会撞上冷缓存
    warm_cache_time: str = "*/25 * * * *"

    # --- 榜单订阅 ---
    rank_page: int = 0
    rank_type: str = ""
    # 同步哪些厂牌的新片。只把番号收进库，不订阅也不下载
    brand_type: str = ""
    # 自动订阅哪些厂牌的新片，会进入下载流程。与 brand_type 独立
    brand_subscribe: str = ""
    # 只订阅最近几天的，避免首次配置时把整段历史都订上
    brand_subscribe_days: int = 3
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
    # 关掉后 BT 源仍参与搜索、结果可见，但自动下载不会选它的种子。
    # 适合来源不稳定、只想手动挑的场景
    bt_auto_download: bool = True

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

    # --- 单点登录 (OIDC) ---
    # 总开关。关掉后即使填了凭证也不显示 SSO 登录
    oidc_enabled: bool = False
    # 提供商地址，用它拼 /.well-known/openid-configuration
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # 登录按钮上的文案
    oidc_display_name: str = "SSO"
    # 请求的 scope，一般不用改
    oidc_scope: str = "openid profile email"
    # Claim 映射：从 userinfo 的哪个字段取用户名
    oidc_username_claim: str = "preferred_username"
    oidc_email_claim: str = "email"
    oidc_name_claim: str = "name"
    # 所有 SSO 用户都登录到这个本地账号。留空则用 username claim 的值，
    # 单用户部署通常填 admin
    oidc_bind_username: str = ""

    # --- Passkey (WebAuthn) ---
    # Relying Party ID，必须是站点域名（不带端口与协议）。留空则从请求推断
    webauthn_rp_id: str = ""
    webauthn_rp_name: str = "cinefold"

    # --- 其他 ---
    javdb_host: str = "https://javdb.com"
    secret_key: str = ""
    # 检测新版本用。公开仓库可留空；配上能把 GitHub API 的匿名限额
    # （60 次/小时）抬到 5000，也是私有镜像拉标签的凭证
    github_token: str = ""
    # GitHub 加速代理，形如 https://edgeone.gh-proxy.org/。填了之后检查更新与
    # 下载更新包都会把 github.com / api.github.com 的地址套上这个前缀，
    # 国内直连 GitHub 不通时用。留空走直连
    github_proxy: str = ""
    # 走 GitHub 代理时是否把 Token 一并发出去。默认不发 —— 代理是第三方的
    # 机器，不该看到凭证。但私有仓库不带 token 就是 404，代理等于白配，
    # 这种情况下只能打开它，前提是你信任那台代理
    github_proxy_send_token: bool = False

    # --- 更新 ---
    # 打开后，检测到带更新包的新版本会自动下载安装并重启。
    # 默认关闭：热更新会重启进程，正在跑的任务会被打断，
    # 什么时候更新该由人来定
    auto_update_enabled: bool = False
    # 自动检查新版本的间隔（分钟）。开关关着时这个任务空转即返回，
    # 手动点检查不受它限制
    update_check_interval: int = 360

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
    if not os.getenv("CINEFOLD_DISABLE_FALLBACK_ENV") and any(
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
    elif not os.getenv("CINEFOLD_DISABLE_FALLBACK_ENV"):
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

        medialink_library_path=_env("MEDIALINK_LIBRARY_PATH"),
        medialink_delete_enabled=_env_bool("MEDIALINK_DELETE_ENABLED", False),
        medialink_scrape_dir=_env("MEDIALINK_SCRAPE_DIR"),
        medialink_webhook_token=_env("MEDIALINK_WEBHOOK_TOKEN"),

        watchdir_delete_grace=_env_int("WATCHDIR_DELETE_GRACE", 1800),
        watchdir_auto_sync=_env_bool("WATCHDIR_AUTO_SYNC", True),
        watchdir_sync_interval=_env_int("WATCHDIR_SYNC_INTERVAL", 30),

        qbittorrent_url=_env("QBITTORRENT_URL"),
        qbittorrent_username=_env("QBITTORRENT_USERNAME"),
        qbittorrent_password=_env("QBITTORRENT_PASSWORD"),
        qbittorrent_apikey=_env("QBITTORRENT_APIKEY"),
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
        telegram_receive_mode=_env("TELEGRAM_RECEIVE_MODE", "webhook").lower(),

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
        primary_site=_env("PRIMARY_SITE"),
        max_actor=_env_int("MAX_ACTOR", 3),

        sync_hot_time=_env("SYNC_HOT_TIME", "0 */2 * * *").strip("'\""),
        sync_brands_time=_env("SYNC_BRANDS_TIME", "10 */2 * * *").strip("'\""),
        sync_actors_time=_env("SYNC_ACTORS_TIME", "20 */2 * * *").strip("'\""),
        fill_empty_image_time=_env("FILL_EMPTY_IMAGE_TIME", "30 */12 * * *").strip("'\""),
        sync_news=_env("SYNC_NEWS", "45 */5 * * *").strip("'\""),
        rank_schedule_time=_env("RANK_SCHEDULE_TIME", "0 20 * * *").strip("'\""),
        brand_schedule_time=_env("BRAND_SCHEDULE_TIME", "30 20 * * *").strip("'\""),
        actor_schedule_time=_env("ACTOR_SCHEDULE_TIME", "0 21 * * *").strip("'\""),
        download_schedule_time=_env("DOWNLOAD_SCHEDULE_TIME", "0 22 * * *").strip("'\""),
        warm_cache_time=_env("WARM_CACHE_TIME", "*/25 * * * *").strip("'\""),

        rank_page=_env_int("RANK_PAGE", 0),
        rank_type=_env("RANK_TYPE"),
        brand_type=_env("BRAND_TYPE"),
        brand_subscribe=_env("BRAND_SUBSCRIBE"),
        brand_subscribe_days=_env_int("BRAND_SUBSCRIBE_DAYS", 3),
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
        bt_auto_download=_env_bool("BT_AUTO_DOWNLOAD", True),

        database_url=_env("DATABASE_URL"),
        db_pool_size=_env_int("DB_POOL_SIZE", 5),
        db_max_overflow=_env_int("DB_MAX_OVERFLOW", 10),
        db_echo=_env_bool("DB_ECHO", False),

        redis_url=_env("REDIS_URL"),
        redis_cache_ttl=_env_int("REDIS_CACHE_TTL", 86400),
        redis_job_store=_env_bool("REDIS_JOB_STORE", False),

        oidc_enabled=_env_bool("OIDC_ENABLED", False),
        oidc_issuer=_env("OIDC_ISSUER").rstrip("/"),
        oidc_client_id=_env("OIDC_CLIENT_ID"),
        oidc_client_secret=_env("OIDC_CLIENT_SECRET"),
        oidc_display_name=_env("OIDC_DISPLAY_NAME", "SSO"),
        oidc_scope=_env("OIDC_SCOPE", "openid profile email"),
        oidc_username_claim=_env("OIDC_USERNAME_CLAIM", "preferred_username"),
        oidc_email_claim=_env("OIDC_EMAIL_CLAIM", "email"),
        oidc_name_claim=_env("OIDC_NAME_CLAIM", "name"),
        oidc_bind_username=_env("OIDC_BIND_USERNAME"),

        webauthn_rp_id=_env("WEBAUTHN_RP_ID"),
        webauthn_rp_name=_env("WEBAUTHN_RP_NAME", "cinefold"),

        javdb_host=_env("JAVDB_HOST", "https://javdb.com"),
        secret_key=_env("SECRET_KEY"),
        github_token=_env("GITHUB_TOKEN"),
        github_proxy=_env("GITHUB_PROXY"),
        github_proxy_send_token=_env_bool("GITHUB_PROXY_SEND_TOKEN", False),

        auto_update_enabled=_env_bool("AUTO_UPDATE_ENABLED", False),
        update_check_interval=_env_int("UPDATE_CHECK_INTERVAL", 360),
    )

    # SECRET_KEY 为空时生成并回写，保证重启后 JWT 仍然有效
    if not s.secret_key:
        s.secret_key = generate_secure_random_string()
        save_setting("SECRET_KEY", s.secret_key)

    _protect_lan_from_proxy()
    return s


# 私有网段与本机，这些地址永远不该走代理
_LAN_NO_PROXY = (
    "localhost", "127.0.0.1", "::1",
    "10.*", "172.16.*", "172.17.*", "172.18.*", "172.19.*",
    "172.20.*", "172.21.*", "172.22.*", "172.23.*", "172.24.*",
    "172.25.*", "172.26.*", "172.27.*", "172.28.*", "172.29.*",
    "172.30.*", "172.31.*", "192.168.*",
)


def _protect_lan_from_proxy() -> None:
    """把私有网段追加进 NO_PROXY。

    配了 HTTP_PROXY 之后，requests / httpx 会连内网请求一起往代理送 ——
    qBittorrent、Emby 这些通常就在同一个局域网里，转出去必然超时，
    报错还长得像"下载器挂了"，很难往代理上想。

    只追加不覆盖：用户自己写的 NO_PROXY 保留在前面。
    """
    if not any(os.environ.get(k) for k in
               ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                "ALL_PROXY", "all_proxy")):
        return

    for key in ("NO_PROXY", "no_proxy"):
        existing = [x.strip() for x in os.environ.get(key, "").split(",") if x.strip()]
        merged = existing + [h for h in _LAN_NO_PROXY if h not in existing]
        os.environ[key] = ",".join(merged)


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
