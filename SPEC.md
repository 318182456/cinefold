# cinefold 规格书

> 本文档记录项目的架构、数据模型与接口约定，作为实现依据。

## 1. 项目定位

影片资源自动化订阅下载器。核心链路：

```
资源站点抓取 → 番号入库 → 定时订阅 → PT/BT 搜种 → 过滤排序 → 下载器推送 → 媒体库校验 → 消息通知
```

## 2. 运行架构

| 组件 | 说明 |
|---|---|
| nginx | 监听 `3750`，静态前端 + `/api/` 反代 + `/pic/` 图片托管 |
| FastAPI | 监听 `127.0.0.1:56168`，`uvicorn` 启动 |
| supervisor | 同时拉起 nginx 与 fastapi |
| SQLite | 通过 SQLAlchemy 2.x 访问 |
| APScheduler | 定时任务调度 |

前端 Vue 3 + Vite，构建产物置于 `/usr/share/nginx/html`。

## 3. 数据模型

### Code（番号主表）
```
code            番号（主键）
title           原始标题
cn_title        中文标题（翻译后）
status          订阅/下载状态
release_date    发行日期
duration        时长
producer        制作商
publisher       发行商
series          系列
genres          类别标签
casts           出演演员
star            评分
banner          横幅图 URL
local_banner    本地横幅路径
still_photo     剧照 URL
local_still_photo  本地剧照路径
poster          封面
preview_url     预览视频
mode            模式
create_time / update_time
```

### Actor（演员表）
```
name            演员名（主键）
name_2          别名
photo           头像
limit_date      订阅起始日期
create_time / update_time
```

### History（下载历史）
```
code            番号
hash            种子 hash
save_path       保存路径
create_time
```

### User（用户表）
```
username / password / token
```

### Cache（通用缓存）
```
id (autoincrement) / namespace / key / content / create_time
```

## 4. API 端点

前缀 `/api/v1`。

| 端点 | 说明 |
|---|---|
| `POST /login` | 登录，返回 JWT |
| `GET  /user/token` | 获取/初始化 token |
| `GET  /dashboard` | 仪表盘统计 |
| `GET  /config` · `POST /config` | 配置读写 |
| `GET  /version` | 版本信息 |
| `GET  /logs` | 日志查询 |
| `GET  /cron` | 定时任务列表 |
| `POST /task` | 手动触发任务 |
| `GET  /qb/autoheal` · `POST /qb/restart` | qB 自愈状态；手动重启 qB 容器（不看开关与冷却期） |
| `GET  /search` | 番号搜索 |
| `GET  /rank` | 排行榜 |
| `GET  /hot` | 热门 |
| `GET  /brands` | 厂牌榜单 |
| `GET  /actors` · `POST /actors/sub` · `GET /actors/rank` | 演员订阅 |
| `GET  /codes/list` | 番号列表 |
| `POST /codes/sub` | 订阅番号 |
| `GET  /codes/release_today` | 今日发行 |
| `GET  /codes/recommend` | 推荐 |
| `POST /codes/download/all` | 批量下载 |
| `GET  /image-proxy` | 图片代理 |
| `GET/POST /message` | 消息回调（Telegram/企业微信 webhook） |
| `GET  /agent/status` | AI 助手是否可用 |
| `POST /agent/chat` | AI 助手对话 |
| `POST /agent/confirm` · `POST /agent/cancel` | 确认/放弃助手提出的下载器操作（confirm 可带 `delete_files` 覆盖删除方式） |

## 5. 模块清单

### 下载客户端 `modules/downloadclient/`
统一接口：`__init__` / `login_*` / `add_torrent` / `add_torrent_by_magnet` / `monitor_torrent`
`control_torrent(action, hashes)` — pause/resume/recheck/reannounce，迅雷未实现
- `qbittorrent` — QBitTorrentClient（另有 `export_torrent` / `get_torrent_detail`，供转移做种用）
- `transmission` — TransmissionClient（另有 `add_torrent_for_seeding`，接管已有文件继续做种）
- `thunder` — Thunder（另有 `get_device_id` / `get_pan_auth` / `analyze_size`）

### 资源站点 `modules/ladysite/`
- `javdb` — Avdb，三种抓取模式：`crawler_*_original` / `_api` / `_bypass`
- `bus` — javbus
- `avbase` — avbase.net，演员与作品
- `library` — javlibrary 排行
- `brands` — 厂牌官网（s1s1s1 / moodyz / ideapocket 等 9 家）
- `jable` — 标签抓取

### PT 站点 `modules/ptsite/`
统一接口：`search` / `download_seed`，部分有 `check_status` / `convert_torrent`
- `mteam`（API Key）· `rousi` · `ptt` · `nicept`（Cookie）
- 公共工具：`search_pt` / `crawling` / `convert_to_mb` / `extract_torrent_size` / `download_seed_by_url`

### 媒体服务器 `modules/mediaserver/`
统一接口：`__init__` / `search`
- `emby` · `jellyfin` · `plex`

### 通知 `modules/notify/`
- `telegram` — 文本/图片/回复消息
- `wechat` — 企业微信，含 `WXBizMsgCrypt3` 加解密

### 翻译 `modules/translate/`
- `baidu` · `google` · `translateai`（OpenAI 兼容）

### 字幕 `modules/subtitle/`
统一接口：`search(code)` → `SubtitleItem | None`，按 `SUBTITLE_SITES` 顺序命中即停。
- `subtitlecat` — 主力，搜索页取详情页再挑下载链接
- `github` — GitHub 字幕仓库，按番号命名，作 subtitlecat 跑路时的兜底

**只认简体中文**。站点的语言标注不可信（大量繁体与机翻日文标成 Chinese），
标注只用来缩小候选，最终按正文判定：汉字数量 → 假名占比 → 简繁字形计数
（`base.is_simplified_chinese`）。判不出就放弃 —— 媒体库里出现看不懂的字幕
比没有字幕更麻烦。编码按字节猜（`decode_subtitle`），站点常透传 GBK/BIG5。

两个字幕源登记在 `datasource` 表里（地址可在页面上改），但 `parser` 留空、
`kind="subtitle"`，因此不会被 `enabled_parser_sources` 拉进详情抓取链路。

### AI 助手 `modules/agent/`
- `agent` — ChatAgent，OpenAI 兼容接口 + function calling，最多 5 轮工具调用
- `tools` — 只读工具：`overview` / `query_codes` / `code_detail` / `list_actors` /
  `list_tasks` / `read_logs` / `list_downloads` / `check_config`；
  另有 `propose_action` 登记待确认的下载器操作
- `actions` — 提案的登记与执行。内存存储、5 分钟过期、一次性消费；
  执行才调下载器的 `control_torrent` / `delete_torrent`，
  或走 `seedtransfer.transfer_hashes`（`transfer` 动作）
- 配置独立于翻译（`AGENT_*`），留空则回退到 `OPENAI_*`

### 云盘 `modules/cloudnas/`
- CloudDrive2 gRPC，离线下载

## 6. 业务服务层 `services/`

```
搜索       search_code / search_actor / search_torrents / find_torrent
补全       fill_lack_codes / fill_lack_actors (+ _by_list 变体)
下载       download_torrent / run_sub_task / run_run_actor
翻译       translate_title / translate_codes
字幕       subtitle.fetch_for_code / subtitle.fill_lack_subtitles
媒体库     is_exist_server
图片       remove_deleted_photo / batch_remove_deleted_photo / search_code_exist_banner
通知       send_message / send_subscribe_message / send_subscribe_actor_message /
          send_downloading_message / send_downloaded_message / send_complete_message
缓存       get_rank_cache
转移做种   seedtransfer.transfer_hashes / run_auto_transfer / list_candidates
```

**字幕 `services/subtitle.py`** — 抓取与落盘。三条触发路径共用：
刮削登记完成（`register_scrape` 尾部，异常吞掉不影响登记）、定时任务
`fill_subtitles`、页面按钮（`POST /medialinks/subtitle`，`manual=True`
跳过总开关 —— 开关管的是自动行为）。字幕写到影片旁边、与影片同名并带
`.zh-CN` 语言后缀，同一番号的每个硬链接位置都写一份。先写临时文件再
原子改名，避免媒体服务器扫到半截文件。已有同名字幕（含用户手工放的）
默认不覆盖，`force=True` 才覆盖。

**转移做种 `services/seedtransfer.py`** — 把 qb 里已下载完的种子交给 tr 继续做种。
导出 `.torrent` 原文件（磁链拿不到私有站的 metadata），按 `SEED_TRANSFER_PATH_MAP`
换算保存路径后加进 tr 并触发校验；tr 确认接管后才动 qb 的源任务，且只删任务不删文件
—— 两个下载器指向同一份文件。触发方式：定时任务 `transfer_seeds`，或 AI 助手的
`transfer` 提案经用户确认。

**qBittorrent 连接自愈 `services/qbwatchdog.py` + `services/dockerctl.py`** —
qb 卡死（WebAPI 不响应但进程还在）时重启它的容器。`QBitTorrentClient` 的每个方法把
成功/失败报给 `qbwatchdog`，只有连接类异常计数（403/404 这类业务错误说明 qb 活着，
不计），连续 `QB_AUTOHEAL_FAILURES` 次才通过 Docker Engine API 重启
`DOCKER_CONTAINER_QBITTORRENT`，两次重启之间有 `QB_AUTOHEAL_COOLDOWN` 分钟冷却。
一次成功响应即清零；连接失败时还会丢掉 client 迫使下次重新登录。计数只在内存里。
`dockerctl` 用 httpx 直连 Docker API（不引 docker SDK），`trust_env=False`
以免配了 `PROXY` 后把内网请求送去代理。默认关闭 —— 重启会打断正在下载的任务。

## 7. 定时任务 `scheduler/`

| 任务 | 默认 cron | 说明 |
|---|---|---|
| `sync_hot` | `0 */2 * * *` | 同步热门 |
| `sync_brands` | `10 */2 * * *` | 同步厂牌 |
| `sync_actors` | `20 */2 * * *` | 同步演员 |
| `sync_news` | `45 */5 * * *` | 同步新片 |
| `fill_empty_banner` | `30 */12 * * *` | 补全缺图 |
| `sub_rank` | `0 20 * * *` | 排行榜订阅 |
| `run_actors` | `0 21 * * *` | 演员订阅 |
| `run_codes_task` | `0 22 * * *` | 订阅下载 |
| `fill_subtitles` | `0 4 * * *` | 补抓字幕（`SUBTITLE_ENABLED` 关闭时直接返回） |

另有 `cache_photos` / `save_image` / `translate_titles` / `pt_wait` / `transfer_seeds` / `push_job` / `start_scheduler` / `restart_scheduler`。

固定间隔任务中，`transfer_seeds`（转移做种，默认 60 分钟，`SEED_TRANSFER_INTERVAL` 可调）
在 `SEED_TRANSFER_ENABLED` 关闭时直接返回，不打下载器接口。

## 8. 过滤与排序 `utils/filters`

```python
filter_torrents(torrents, filter_config)   # 按 DEFAULT_FILTER 规则过滤
sort_torrents(torrents, sort_rule)         # 按 DEFAULT_SORT 多级排序
has_chinese(title) / has_uc(title) / has_uhd(title)
```

**过滤项**：`only_chinese` `only_uc` `exclude_uc` `only_uhd` `exclude_uhd` `only_free` `include_keywords` `exclude_keywords` `min_size` `max_size`

**排序键**：`free,chinese,uc,!uc,site,seeders,!uhd,uhd`（`!` 前缀表示降权）

## 9. Torrent 数据结构

```python
id: int              # 唯一标识
site: str            # 来源站点
title: str           # 标题
size_mb: float       # 大小（MB）
seeders: int         # 做种数
chinese: bool        # 中文字幕
uc: bool             # 无码破解
uhd: bool            # 4K/8K
free: bool           # 免费
download_url: str    # 磁链或种子地址
```

## 10. 未实现的配置项

以下配置项保留占位以兼容既有 `.env`，但不生效：

- `FLARE_SOLVERR_URL` / `crawler_*_bypass` 系列 —— 反爬绕过统一走 `BYPASS_URL`
- `ENABLE_BT_ANTI_LEECH` —— BT 防吸血

## 11. 技术栈

Python 3.12 · FastAPI · SQLAlchemy 2.x · APScheduler · httpx · qbittorrent-api · loguru · Vue 3 + Vite
