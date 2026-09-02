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
subscribed      是否用户主动订阅
create_time / update_time
```

**这张表一行有两种身份，靠 `subscribed` 区分：**

- `subscribed = true` —— 用户主动订阅，`run_actors` 每轮拿它去刷新作品
- `subscribed = false` —— 演员资料缓存（头像、别名），只供搜索和作品页读

**导入不等于订阅。** 任何批量导入（爬虫库 `import_casts`、SQLite→PG 迁移、
今后新增的数据源）写进 actor 表的行，一律 `subscribed = false`；
只有 `POST /actors/sub` 走的 `subscribe_actor` 才能把它置真。

违反这条的后果是实测过的：爬虫库导入每轮全量灌进上千条演员，
在旧语义（有行即订阅）下全成了「已订阅」，`run_actors` 于是每晚
替全库演员刷新作品 —— 界面上凭空多出 1333 条订阅，就是这么来的。

配套的几处约束：

- `import_casts` 有 `ACTOR_PROTECTED_FIELDS`（`name` / `limit_date` /
  `subscribed`），导入只补资料字段，这三列一概不碰
- `cancel_actor` 只把标记翻假，不删行 —— 行本身还是资料缓存，
  删了下轮导入照样写回来
- 对外报「已订阅演员」数量的地方（列表页、看板统计、AI 助手）
  都要按 `subscribed` 筛，否则会把资料缓存一起算进去
- 番号侧的同类约束见 `cache_remote_codes`：导入不动 `status`，
  新建的行落 `CodeStatus.NONE`，同样是「导入不等于订阅」

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

### Review（AI 影评）
```
code            番号（主键）
cast_count      出演人数，按 casts 实际条数算，不信模型
body_type       身材描述，依据不足时为空
style           拍摄风格，依据不足时为空
highlights      看点要点，换行分隔
summary         简评正文
nfo_time        写进 NFO 的时间。空表示还没写出，定时任务据此补写
create_time / update_time
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
| `GET  /medialinks/review` · `POST /medialinks/review` | 读取/生成 AI 影评（POST 的 `manual=True` 跳过总开关，`force` 重新生成）|
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
- `local` — 本地字幕素材库（`subtitlelocal`），命中直接取
- `javsub` — JavSub.ai（`javsub`），收录全面的 AI 翻译字幕站，支持 Free/Demo 下载与搜索
- `subtitlecat` — SubtitleCat（`subtitlecat`），搜索页取详情页再挑下载链接
- `github` — GitHub 字幕仓库（`subtitlegh`），按番号命名，作兜底

**只认简体中文**。站点的语言标注不可信（大量繁体与机翻日文标成 Chinese），
标注只用来缩小候选，最终按正文判定：汉字数量 → 假名占比 → 简繁字形计数
（`base.is_simplified_chinese`）。繁体通过 `as_simplified_chinese` 自动规整为简体，
判不出就放弃 —— 媒体库里出现看不懂的字幕比没有字幕更麻烦。
编码按字节猜（`decode_subtitle`），站点常透传 GBK/BIG5。

字幕源登记在 `datasource` 表里（地址可在页面上改），但 `parser` 留空、
`kind="subtitle"`，因此不会被 `enabled_parser_sources` 拉进详情抓取链路。

### AI 影评 `modules/review/`
- `reviewai` — OpenAI 兼容接口，提示词要求只按给定证据归纳，依据不足留空。
  用哪套 AI 由 `REVIEW_PROVIDER` 决定（`_pick_provider`）：`auto` 先助手后翻译，
  `agent` / `translate` 指定死。**整套取而不是逐字段回退** —— 助手只填了地址
  没填 Key 时，逐字段回退会拿助手地址配翻译 Key 发出去，必然 401 且日志里
  看着两处都「配了」，极难查。指定死的那两个即使没配全也不换套
- `profile` — 演员/厂牌画像聚合，从库里同演员、同厂牌的历史作品统计高频标签

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
默认不覆盖，`force=True` 才覆盖。硬链接列表带 `has_subtitle` 字段（探测走
30 秒 TTL 缓存 + 并发，抓完显式失效），页面据此标「有字幕」，多个位置只命中
部分时标成 `字幕 1/2` —— 换个入口播放就没有，得看得出来还要补。

**AI 影评 `services/review.py`** — 按元数据生成看点并写出。三条触发路径与
字幕同构：刮削登记完成（`register_scrape` 尾部，异常吞掉不影响登记）、定时
任务 `fill_reviews`、页面按钮（`POST /medialinks/review`，`manual=True` 跳过
总开关）。模型没看过影片，依据限定为本片元数据 + 画像聚合
（`modules/review/profile.py`：同演员、同厂牌在库里其它作品的高频标签，
带命中数 `hits/total` 一起交给模型；单次命中当噪声丢掉）。出演人数不信模型，
按 `casts` 实际条数算。依据不足的字段留空——提示词把「不许编」钉死。
结果落 `review` 表后写向两处：影片旁 NFO 的 `<plot>`（只改这一个节点，
带 `【AI】`…`▲` 首尾标记以便重新生成时只替换自己那段；解析失败不碰、
文件不存在不新建）与 Emby 条目的 `Overview`（先 GET 完整条目再整体 POST
回去——该接口是覆盖语义，只发单个字段会清空其余元数据）。写 NFO 成功才记
`nfo_time`。

渲染上有两条硬约束，都因为客户端简介栏折叠后只露前三四行：AI 段**排在官方简介之前**（`merge_text`），且压到 2～3 行（标记不单占行、要点全折进一行）。要首尾两个标记而不是只有开头一个，是因为段落在前面 —— 只有开头标记时切分会把后面的官方简介一起切掉。早先用过的标记记在 `LEGACY_MARKERS` / `LEGACY_END_MARKERS`，剥离时一并识别，否则存量条目会并排躺着两段 AI 内容；这两张表只能加不能删。

定时任务 `_rewrite_pending` 的补写判据是**读文件看 MARKER 在不在**，
不是只看 `nfo_time` 为空 —— 刮削工具重刮会把 plot 整个冲掉，而库里的时间戳
还留着上次写成功的值，只信时间戳的话这类记录永远选不中，看点就此永久丢失。
补写不发 AI 请求（内容库里都有），只是把它写回去。

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
| `fill_reviews` | `35 * * * *` | 补生成 AI 影评（`REVIEW_ENABLED` 关闭时直接返回） |

另有 `cache_photos` / `save_image` / `translate_titles` / `pt_wait` / `transfer_seeds` / `push_job` / `start_scheduler` / `restart_scheduler`。

固定间隔任务中，`transfer_seeds`（转移做种，默认 60 分钟，`SEED_TRANSFER_INTERVAL` 可调）
在 `SEED_TRANSFER_ENABLED` 关闭时直接返回，不打下载器接口。每个固定间隔任务都有
对应的 `*_INTERVAL` 配置项（分钟数），没有配置项的任务在任务页上改不了 —— 改了
也没处存，重启就退回默认值。

### 自然语言排班

`app/services/schedule.py` 把「每天凌晨 4 点」这类说法翻成 cron，任务页用它做
输入。规则优先、AI 兜底：常见说法走本地正则（不发请求，AI 没配也能用），规则
认不出来才问 AI；两条路都不认就返回 `None`，由调用方提示换个说法，绝不猜一个
表达式存进去 —— 排班猜错了不报错，只会在某个没人盯着的时刻悄悄跑错。

`describe_cron` / `describe_interval` 做反向回读，供界面显示与编辑时预填。
两个方向必须互为逆运算（见 `tests/test_schedule.py::test_roundtrip_is_stable`）：
界面预填的就是回读文本，回读的说法要是翻不回原来的 cron，用户什么都没改、
只点了保存，排班就悄悄变了。

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
free: bool           # 完全不计下载量（过滤与排序只认它）
discount: str        # 折扣标识：free / 2x_free / percent_50 / percent_30 / 2x，无折扣为空
download_url: str    # 磁链或种子地址
```

`discount_label` 是只读属性，把 `discount` 译成展示文案（`percent_50` → `50%`）；
站点没给 `discount` 但标了 `free` 时退回「免费」。搜索列表的标记列、下载通知
和「选中」日志都用它。

## 10. 未实现的配置项

以下配置项保留占位以兼容既有 `.env`，但不生效：

- `FLARE_SOLVERR_URL` / `crawler_*_bypass` 系列 —— 反爬绕过统一走 `BYPASS_URL`
- `ENABLE_BT_ANTI_LEECH` —— BT 防吸血

## 11. 技术栈

Python 3.12 · FastAPI · SQLAlchemy 2.x · APScheduler · httpx · qbittorrent-api · loguru · Vue 3 + Vite
