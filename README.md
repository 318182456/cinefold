# cinefold

影片资源自动化订阅下载器。抓取番号 → 按规则过滤 → 推送下载器 → 校验入库 → 消息通知，
并支持与媒体库联动清理。

---

## 功能

| 类别 | 支持 |
|---|---|
| 下载器 | qBittorrent（含 5.x）· Transmission · 迅雷 |
| 媒体库 | Emby · Jellyfin · Plex |
| PT 站点 | M-Team · Rousi · PTT · NicePT · 自定义 BT 源 |
| 资源站 | JavDB · JavBus · JavLibrary · 厂牌官网（9 家） |
| 通知 | Telegram · 企业微信（含指令回调） |
| 翻译 | OpenAI 兼容接口 · 百度 · Google |

---

## 部署

### Docker Compose（推荐）

```bash
git clone <repo> cinefold && cd cinefold

# 前端产物需先构建，或直接使用仓库中已有的 web/dist
cd web && npm install && npm run build && cd ..

docker compose up -d --build
```

访问 `http://<host>:3750`。

**首次启动的账号密码在日志里**：

```bash
docker compose logs | grep 初始账号
# 已创建初始账号 —— 用户名: admin  密码: xxxxxxxx  请登录后立即修改
```

### 直接运行

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export DATA_DIR=./data CONFIG_DIR=./data/config LOG_DIR=./data/logs
.venv/bin/python main.py     # 监听 56168
```

前端需另行构建并由 nginx 托管，参考 [deploy/nginx.conf](deploy/nginx.conf)。

---

## 配置

配置文件位于 `${CONFIG_DIR}/.env`（容器内为 `/data/config/.env`），首次启动会从模板生成。
也可在 Web UI 的**设置**页直接修改，保存后自动重载调度器。

本地开发时，项目根目录的 `app.env` 会作为备用配置源自动读取。

### 必填项

```bash
# 下载器（至少配一个）
QBITTORRENT_URL=http://192.168.1.10:8080
QBITTORRENT_USERNAME=admin
QBITTORRENT_PASSWORD=xxx
QBITTORRENT_DOWNLOAD_PATH=/downloads/av
QBITTORRENT_CATEGORY=av

# 资源来源（至少配一个 PT 站或 BT 源）
MTEAM_API_KEY=xxx
```

完整配置项见 [app/core/.env.example](app/core/.env.example)。

### 过滤与排序

```bash
DEFAULT_FILTER='{"only_chinese": false, "exclude_uhd": true, "exclude_vr": true, "min_size": "5GB", "max_size": "10GB"}'
DEFAULT_SORT='free,chinese,uc,!uc,site,seeders,!uhd,uhd'
PRIMARY_SITE=MTeam
```

- **过滤**：不满足的种子直接排除
  - `only_*` 只要、`exclude_*` 排除，可选属性 `chinese` `uc` `uhd` `vr` `free`
  - `min_size` / `max_size` 支持 `5GB` 写法，无单位时按 MB
  - `min_seeders` / `max_seeders` 做种数区间，留空或 0 表示不限
  - `include_keywords` / `exclude_keywords` 逗号分隔，匹配标题
- **排序**：多个种子都满足时选哪个。逗号分隔，越靠前优先级越高，
  `!` 前缀表示降权（`!uhd` = 优先非 4K）。可用键：
  `free` `chinese` `uc` `uhd` `vr` `seeders` `size` `site`
- **主站**：`PRIMARY_SITE` 指定优先选哪个 PT 站，逗号分隔可给多个。
  仅在 `DEFAULT_SORT` 含 `site` 时生效，且排在它前面的键优先级更高

设置页「过滤与排序」可视化配置这些项，无需手写 JSON。

### 网络

```bash
PROXY=socks5://user:pass@host:7890
# 自建的反爬服务，直连遇到 403/503 时自动改走它
BYPASS_URL=http://host:8191/v1
```

`BYPASS_URL` 兼容 FlareSolverr（`POST /v1`）与 cloudflare-bypass-for-scraping（`GET /html`），会自动识别类型。

**推荐用 FlareSolverr**。实测各站点情况：

| 站点 | 直连（经代理） | 经 FlareSolverr |
|---|---|---|
| javbus | 200 正常 | — |
| javdb | 403 Cloudflare | 正常 |
| javlibrary | 403 Cloudflare | 正常 |

过盾需要启动真实浏览器，单次请求可能耗时 30–60 秒，属正常现象。抓取任务由定时器驱动，不影响使用。

---

## 自定义 BT 源

配置 `BT_URL` 接入任意接口，返回格式：

```json
{
  "data": [
    {
      "id": 1001,
      "site": "BT",
      "title": "SSIS-001 中文字幕",
      "size_mb": 2048.5,
      "seeders": 50,
      "chinese": true,
      "uc": false,
      "uhd": false,
      "free": true,
      "download_url": "magnet:?xt=urn:btih:..."
    }
  ]
}
```

`BT_URL` 与 `BT_JSON_DATA` 中的 `${keyword}` 会被替换成番号；URL 无占位符时自动追加 `?keyword=`。

---

## 定时任务

| 任务 | 默认时间 | 说明 |
|---|---|---|
| 订阅下载 | `0 22 * * *` | 已订阅番号推送下载器 |
| 演员订阅 | `0 21 * * *` | 订阅演员的新作品入队 |
| 榜单订阅 | `0 20 * * *` | 抓榜单并自动订阅（需 `RANK_PAGE > 0`） |
| 同步热门 | `0 */2 * * *` | — |
| 同步厂牌 | `10 */2 * * *` | 需配 `BRAND_TYPE` |
| 同步演员 | `20 */2 * * *` | 补全头像 |
| 同步新片 | `45 */5 * * *` | — |
| 补全缺图 | `30 */12 * * *` | 补详情与封面 |
| 同步下载状态 | 每 5 分钟 | 固定间隔 |
| 翻译标题 | 每 30 分钟 | 固定间隔 |

任务页可手动触发任意任务。

---

## 消息指令

配置 Telegram Bot 后可用以下指令：

```
/sub 番号      订阅
/cancel 番号   取消订阅
/search 番号   搜索资源
/status        查看统计
/help          帮助
```

直接发送番号等同于 `/sub`，一条消息里写多个也能识别。建议配置
`TELEGRAM_WHITELIST` 限制可用用户。

### 接收上行消息

要让 bot 收到消息，需二选一（`TELEGRAM_RECEIVE_MODE`）：

| 方式 | 适用场景 | 配置 |
| --- | --- | --- |
| `webhook` | 有公网 HTTPS 地址 | 填 `EXTERNAL_DOMAIN`，在设置页点「设置 Webhook」 |
| `polling` | 家宽 / NAS，无公网地址 | 选 polling 保存即可，后台自动长轮询 |

Webhook 要求 HTTPS，端口限 443/80/88/8443。两种方式互斥，切到 polling
会自动删除已有 webhook。

设置页「通知」分组可查看当前状态：webhook 地址、堆积消息数、最近回调错误。
Bot 能推送但收不到消息时，先看这里。

---

## 媒体联动删除

在 Emby / Jellyfin 里删掉一部影片，联动删除源文件、种子任务和刮削产物。

### 前置条件

三项缺一条都不会生效：

1. **打开「设置 → 媒体联动 → 启用联动删除」**，保存即生效，不必重启。
   **默认关闭** —— 此时接口照常返回 200、日志照常打印，但一个文件都不会删
   （内部降级成演练）。日志里出现 `联动删除未启用...仅记录不执行`
   就是这个开关还没开。也可以用 `MEDIALINK_DELETE_ENABLED=true` 从环境变量配。
2. Emby 装 Webhooks 插件，事件勾选删除类，地址与密钥在同一个面板里 ——
   「删除联动回调」那一栏已经把完整地址拼好，直接复制；密钥点「生成随机密钥」，
   明文只在保存前显示一次，务必先存下来再保存。
3. Emby 容器与 cinefold 容器的媒体目录**挂载路径保持一致**。不一致时只能靠
   文件名兜底匹配，同名文件多了会误伤。

同一分组里另外两项也在这里配：**媒体库根目录**（按 inode 反查硬链接的扫描
范围）和**刮削输出目录**（该目录受保护，不会被当成空目录清掉）。

接入前先演练一次，确认命中的文件和种子都对：

```bash
curl -X POST "http://host:3750/api/v1/webhook/emby?dry_run=1" \
  -H 'Content-Type: application/json' \
  -H 'X-Cinefold-Token: <token>' \
  -d '{"Event":"item.remove","Item":{"Path":"/media/ABS-001/ABS-001.mp4"}}'
```

`<token>` 就是上面那个 Webhook 密钥；留空未设时可以省掉这个请求头。

返回里 `torrents_deleted` / `files_deleted` / `links_deleted` 都非空才算通。
若报「未找到关联记录」，说明这个文件没登记过 —— 见下。

### 两种目录模式

删除联动的依据是 `media_link` 表里的关联记录。记录怎么来，取决于监控目录用
哪种模式：

| 模式 | 做什么 | 适用 |
| --- | --- | --- |
| 硬链接（默认） | 源目录 → 目标目录建硬链接，Emby 扫目标目录 | 需要刮削、需要分类整理 |
| 直通 | 不建链接，Emby 直接扫源目录 | 不刮削、不整理的内容（短视频等） |

直通模式在添加监控目录时勾选，此时不需要填目标目录。它只做两件事：登记
`media_link`（`link_path` 就是源文件本身）、把种子 hash 落到 `history` ——
目的仅仅是让上面那条删除联动能找到东西。

**直通模式没有硬链接兜底**：Emby 里删掉就是直接删源文件，没有中间态。建议
先不开反向删除，观察一段时间再放开。误删防护只剩两道：inode 移动判定
（在目录里挪动文件不会被当成删除）和宽限期扣留 —— 后者在
「设置 → 监控目录 → 删除宽限期」调，默认 30 分钟，**别设成 0**。

刮削场景下的记录由 `/webhook/scrape` 回调登记，与监控目录无关。历史遗留的
库两者都没走过，表里没有记录，删除时不会有任何动作。

---

## 更新

两条路，各管各的：

| 方式 | 更新范围 | 场景 |
| --- | --- | --- |
| 设置页一键更新 | 应用代码（前端 + 后端） | 日常，不用碰宿主机 |
| `docker compose pull` | 整个镜像，含系统依赖 | 换 Python 版本、加系统包等 |

### 一键更新

设置页「其他 → 版本与更新」显示当前版本与最新版本，点更新即可。程序会从
GitHub Releases 下载 `backend-x.y.z.zip` 与 `frontend-x.y.z.zip`，校验 sha256、
补 Python 依赖、拿新代码试跑一次导入，全部通过才写入并重启。任何一步失败都
不会动生效目录，跑的还是旧版本。

更新包装在 `${DATA_DIR}/updates/`（容器内 `/data/updates`），是挂载卷 ——
`docker compose pull` 重建容器之后它还在。启动时程序会比较两个版本：

- overlay 更新 → 用 overlay
- 镜像追平或反超 → 用镜像，并顺手删掉过期的 overlay

所以拉了新镜像不会被旧的热更新代码盖住，两条路不打架。

更新后出问题点「回退上一版」，退回上次热更新前的版本并重启。第一次热更新
之前跑的是镜像自带代码，此时回退等于删掉 overlay 退回镜像版本。

### 自动更新

设置页打开「自动更新」（或 `AUTO_UPDATE_ENABLED=true`），检测到带更新包的
新版本就自动装并重启，间隔由 `UPDATE_CHECK_INTERVAL` 控制（分钟，默认 360）。
**默认关闭** —— 重启会打断正在跑的抓取和下载任务。

配 `GITHUB_TOKEN` 可把 GitHub API 的匿名限额（60 次/小时）抬到 5000，公开
仓库不配也能用。

### 走代理

直连 GitHub 不通时，在设置页填「GitHub 代理」（或 `GITHUB_PROXY`），比如
`https://edgeone.gh-proxy.org/`。检查更新和下载更新包都会把 GitHub 的地址
套上这个前缀，其他地址不受影响。

代理是别人的机器，所以默认不会把 `GITHUB_TOKEN` 发出去 —— 限额掉回匿名的
60 次/小时，但凭证不经手第三方。

**私有仓库例外**：GitHub 对没有凭证的请求一律回 404（不是 401），不带 Token
就永远查不到版本，代理等于白配。这种情况下要打开「代理携带 Token」
（`GITHUB_PROXY_SEND_TOKEN=true`），前提是你信得过那台代理。不想把 Token
交出去的话，就别配 GitHub 代理，改用 `PROXY` 走自己的 HTTP/SOCKS 代理。

### 内网地址不走代理

配了 `HTTP_PROXY` 之后，requests 和 httpx 会把内网请求也往代理送 ——
qBittorrent、Emby 这些通常就在同一个局域网里，转出去必然超时，报错还长得
像「下载器挂了」。启动时程序会自动把 `127.0.0.1`、`10.0.0.0/8`、
`172.16.0.0/12`、`192.168.0.0/16`、`169.254.0.0/16` 追加进 `NO_PROXY`，
你自己写的条目会保留在前面。

必须写 CIDR：requests 只认精确主机名、域名后缀和 CIDR，`192.168.*` 里的
`*` 会被当成普通字符，整条规则静默失效。自己往 `NO_PROXY` 加内网条目时
同理，别用通配符。

httpx 比 requests 更严，连 CIDR 都不认（只认精确主机），所以走 httpx 且
目标在内网的模块——Emby、Jellyfin——是在代码里写 `trust_env=False` 直接
不信任环境代理，不依赖 `NO_PROXY`。

### 发版

推 `v0.0.8` 这样的 tag 触发两条流水线：`docker-build.yml` 出镜像，
`release.yml` 出 zip 并挂到 Release。tag 版本号必须与 `VERSION` 文件一致，
否则 CI 直接失败 —— 版本对不上的包装上去会被程序拒绝。

只发了镜像没挂 zip 的版本，界面会提示有更新但不给点安装，需要在宿主机
`docker compose pull`。

---

## API

除登录外均需鉴权，支持两种方式：

```bash
# JWT（有效期 7 天）
curl -X POST http://host:3750/api/v1/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"xxx"}'

# 长期 token（账户页获取，不过期）
curl http://host:3750/api/v1/dashboard -H 'Authorization: Bearer <token>'
```

接口文档：`http://host:3750/api/docs`

---

## 开发

```bash
# 后端
.venv/bin/python -m pytest tests/ -q      # 455 项测试
.venv/bin/python -m uvicorn app.api:app --port 56168 --reload

# 前端
cd web && npm run dev                      # 5173，API 自动代理到 56168
```

目录结构：

```
app/
  api/          FastAPI 路由与鉴权
  core/         配置加载
  database/     模型与会话
  modules/      下载器 / 媒体库 / PT 站 / 资源站 / 通知 / 翻译
  scheduler/    定时任务
  services/     业务编排
  utils/        番号识别、过滤排序、JWT、日志
web/src/        Vue 3 前端
tests/          测试
```

---

## 已知限制

- **JavDB** 只解析公开 HTML 页面，不走需要签名的 App 接口
- **BT 防吸血** —— `ENABLE_BT_ANTI_LEECH` 保留配置位但不生效
- **`BYPASS_URL`** 的调用已实现，但绕过服务本身需要你自行部署

---

## 常见问题

**qBittorrent 登录失败**

qBittorrent 5.x 改了登录协议（响应体 `Ok.` → 204 空响应，Cookie `SID` → `QBT_SID_<端口>`），旧版客户端库无法识别。本项目已兼容。若使用自签证书或反代，保持 `QBITTORRENT_VERIFY_CERT=False`。

**资源站抓不到数据**

按顺序排查：站点是否可直连 → `PROXY` 是否可达 → `BYPASS_URL` 服务是否正常。日志页能看到具体失败原因。

**`JAVDB_HOST` 填什么**

填 HTML 站点地址（默认 `https://javdb.com`）。填成 `apidd.*` 这类 App 接口域名会被自动回退并在日志中提示。

**忘记密码**

删除 `${DATA_DIR}/cinefold.db` 中 `user` 表的记录后重启，会重新生成账号并打印到日志。
