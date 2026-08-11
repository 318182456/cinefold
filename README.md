# byte-muse

影片资源自动化订阅下载器。抓取番号 → 按规则过滤 → 推送下载器 → 校验入库 → 消息通知。

原项目源码丢失，本仓库依据 `envyafish/byte-muse:1.27.4` 镜像的模块结构重写。

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
git clone <repo> byte-muse && cd byte-muse

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
.venv/bin/python -m pytest tests/ -q      # 138 项测试
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

## 与原项目的差异

本重写版**不包含**以下原版特性：

- **License 校验** —— 原版启动时向 `bytemuselincese.zeabur.app` 验证授权码，已移除
- **JavDB App 私有接口** —— 原版通过 `apidd.czssdgz.com` 调用需签名的 JSON 接口，本项目只解析公开 HTML 页面
- **BT 防吸血** —— `ENABLE_BT_ANTI_LEECH` 保留配置位但不生效

`BYPASS_URL` 的调用已实现，但绕过服务本身需要你自行部署。

---

## 常见问题

**qBittorrent 登录失败**

qBittorrent 5.x 改了登录协议（响应体 `Ok.` → 204 空响应，Cookie `SID` → `QBT_SID_<端口>`），旧版客户端库无法识别。本项目已兼容。若使用自签证书或反代，保持 `QBITTORRENT_VERIFY_CERT=False`。

**资源站抓不到数据**

按顺序排查：站点是否可直连 → `PROXY` 是否可达 → `BYPASS_URL` 服务是否正常。日志页能看到具体失败原因。

**`JAVDB_HOST` 填什么**

填 HTML 站点地址（默认 `https://javdb.com`）。填成 `apidd.*` 这类 App 接口域名会被自动回退并在日志中提示。

**忘记密码**

删除 `${DATA_DIR}/byte-muse.db` 中 `user` 表的记录后重启，会重新生成账号并打印到日志。
