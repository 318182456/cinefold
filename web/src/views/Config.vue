<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import {
  checkVersion, deletePasskey, deleteTelegramWebhook, getConfig, getOidcRedirectUri,
  getTelegramReceive, getUpgradeStatus, listPasskeys, listPtSites, passkeyRegisterBegin,
  passkeyRegisterFinish, rollbackUpgrade, saveConfig, setTelegramWebhook, startUpgrade,
  testConnection, testOidc,
} from '@/api'
import { PasskeyCancelled, createCredential, isSupported } from '@/utils/webauthn'
import { useToast } from '@/composables/useToast'
import { useConfigStore } from '@/stores/config'
import ConfigField from '@/components/ConfigField.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'

const toast = useToast()
const configStore = useConfigStore()

const form = reactive({})
const loading = ref(true)
const saving = ref(false)
const activeGroup = ref('downloader')
const testing = ref('')
const testResults = reactive({})

// Telegram 上行消息接收状态
const tgReceive = ref(null)
const tgBusy = ref('')

// PT 主站下拉。取实际配好的站点，空串表示不指定
const ptSiteOptions = ref([''])

// 登录方式
const authOrigin = ref(null)
const oidcRedirectUri = ref('')
const oidcTest = ref(null)
const oidcBusy = ref(false)
const passkeys = ref([])
const passkeyLabel = ref('')
const passkeyBusy = ref(false)
const passkeySupported = isSupported()

const browserOrigin = window.location.origin
// 服务端算出的地址与浏览器不符时，SSO 与 Passkey 必然失败
const originMismatch = computed(
  () => !!authOrigin.value?.origin && authOrigin.value.origin !== browserOrigin,
)

// 媒体联动的两个 webhook 地址。外部工具要填的就是这个，直接拼给用户复制
const medialinkBase = computed(
  () => (form.external_domain || browserOrigin || '').replace(/\/+$/, ''),
)
const medialinkHooks = computed(() => [
  {
    key: 'scrape',
    title: '刮削完成回调',
    url: `${medialinkBase.value}/api/v1/webhook/scrape`,
    hint: '填到刮削工具（MDCng 等）的 webhook。触发事件选「刮削成功」',
  },
  {
    key: 'emby',
    title: '删除联动回调',
    url: `${medialinkBase.value}/api/v1/webhook/emby`,
    hint: 'Emby / Jellyfin 的 Webhook 插件，事件选 item.remove',
  },
])

// MDCng 的 Body 模板。反斜杠不转义也能解析，无需用户处理
const SCRAPE_BODY_TEMPLATE =
  '{"event":"{{ event }}","number":"{{ number }}",'
  + '"source_path":"{{ source_path }}"}'

function copyText(text, label) {
  if (!text) return
  navigator.clipboard?.writeText(text)
    .then(() => toast.success(`已复制${label}`))
    .catch(() => toast.error('复制失败，请手动选择'))
}

// 刚生成、尚未保存的密钥明文。
// 密钥是敏感字段，保存后接口只回传掩码，用户再也看不到原值，
// 而它必须被填进 MDCng 与 Emby，所以在这里留一份明文供当场复制。
const freshWebhookToken = ref('')

function generateWebhookToken() {
  // 这串可能要手抄进别的系统，去掉易混淆的 0 O 1 l I，共 57 个字符
  const alphabet = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
  // 直接对字节取模会偏向字母表前几个字符（256 不是 57 的整数倍），改用拒绝采样
  const limit = Math.floor(256 / alphabet.length) * alphabet.length
  const chars = []

  while (chars.length < 32) {
    const bytes = new Uint8Array(32)
    crypto.getRandomValues(bytes)
    for (const byte of bytes) {
      if (byte >= limit) continue
      chars.push(alphabet[byte % alphabet.length])
      if (chars.length === 32) break
    }
  }

  const token = chars.join('')
  form.medialink_webhook_token = token
  freshWebhookToken.value = token
  toast.success('已生成密钥，保存后生效')
}

// 侧边导航的大类。分组多了平铺一排按钮找不着东西，按用途归到四类下面
const CATEGORIES = [
  { key: 'pipeline', title: '下载链路' },
  { key: 'library', title: '媒体库' },
  { key: 'assist', title: '通知与 AI' },
  { key: 'system', title: '系统' },
]

// 字段类型 t: text(默认) / password / bool / select / textarea / tags / size
// 每个分组按用途拆成若干 section，每个 section 一张卡片
// cat 指向所属大类，决定它在侧边导航里排到哪一栏
const GROUPS = [
  {
    key: 'downloader',
    cat: 'pipeline',
    title: '下载器',
    sections: [
      {
        title: 'qBittorrent',
        fields: [
          { k: 'qbittorrent_url', label: '地址', ph: 'http://192.168.1.10:8080' },
          { k: 'qbittorrent_apikey', label: 'Web API Key', t: 'password', ph: 'qbt_...',
            hint: 'qb 5.2.0+ 可用。填了就走 Bearer 鉴权，不再反复登录，账号密码可留空' },
          { k: 'qbittorrent_username', label: '用户名' },
          { k: 'qbittorrent_password', label: '密码', t: 'password' },
          { k: 'qbittorrent_download_path', label: '下载路径' },
          { k: 'qbittorrent_category', label: '分类' },
          { k: 'qbittorrent_verify_cert', label: '校验 HTTPS 证书', t: 'bool',
            hint: '使用自签证书或反代时保持关闭' },
        ],
      },
      {
        title: 'Transmission',
        fields: [
          { k: 'transmission_url', label: '地址' },
          { k: 'transmission_username', label: '用户名' },
          { k: 'transmission_password', label: '密码', t: 'password' },
          { k: 'transmission_download_path', label: '下载路径' },
          { k: 'transmission_label', label: '标签' },
        ],
      },
      {
        title: '转移做种',
        fields: [
          { k: 'seed_transfer_enabled', label: '自动转移', t: 'bool',
            hint: '定时把 qBittorrent 里已下载完的种子交给 Transmission 继续做种，文件不移动' },
          { k: 'seed_transfer_interval', label: '检查间隔（分钟）', t: 'number', ph: '60' },
          { k: 'seed_transfer_batch_limit', label: '每轮最多转移', t: 'number', ph: '20',
            hint: '每个种子都要触发一次 tr 校验，校验吃磁盘 IO。存量多可调大，填 0 表示不限' },
          { k: 'seed_transfer_delete_source', label: '转移后删除 qB 源任务', t: 'bool',
            hint: '只删任务不删文件。关闭则两个下载器同时做种同一份文件' },
          { k: 'seed_transfer_categories', label: '限定分类', ph: '留空表示不限，多个用逗号分隔' },
          { k: 'seed_transfer_tags', label: '限定标签', ph: '留空表示不限，多个用逗号分隔' },
          { k: 'seed_transfer_label', label: 'Transmission 标签',
            hint: '转移进来的种子打上这个标签，便于区分文件不归 tr 管的那批' },
          { k: 'seed_transfer_path_map', label: '路径映射', ph: '/downloads:/data/downloads',
            hint: '两个下载器挂载点不同时填，格式「qB前缀:tr前缀」，多组用逗号分隔' },
        ],
      },
      {
        title: 'qBittorrent 连接自愈',
        fields: [
          { k: 'qb_autoheal_enabled', label: '连不上时重启 qB', t: 'bool',
            hint: 'qB 卡死（WebAPI 不响应但进程还在）时重启它的容器。会让正在下载的任务断连重试' },
          { k: 'qb_autoheal_failures', label: '连续失败几次后重启', t: 'number', ph: '3',
            hint: '只算连接失败；403、种子不存在这类业务错误不计数，一次成功响应即清零' },
          { k: 'qb_autoheal_cooldown', label: '重启冷却（分钟）', t: 'number', ph: '15',
            hint: 'qB 重启后要几十秒才能响应，冷却期太短会连着重启好几轮' },
          { k: 'qb_autoheal_notify', label: '重启后通知', t: 'bool' },
          { k: 'docker_host', label: 'Docker 地址', ph: 'unix:///var/run/docker.sock',
            hint: '同机用 unix socket，需把 /var/run/docker.sock 挂进容器（不能只读）；qB 在别的机器上填 tcp://IP:2375' },
          { k: 'docker_container_qbittorrent', label: 'qB 容器名', ph: 'qbittorrent' },
        ],
      },
    ],
    tests: ['qbittorrent', 'transmission', 'docker'],
  },
  {
    key: 'media',
    cat: 'library',
    title: '媒体库',
    sections: [
      {
        title: 'Emby',
        fields: [
          { k: 'emby_url', label: '地址' },
          { k: 'emby_api_key', label: 'API Key', t: 'password' },
        ],
      },
      {
        title: 'Jellyfin',
        fields: [
          { k: 'jellyfin_url', label: '地址' },
          { k: 'jellyfin_api_key', label: 'API Key', t: 'password' },
          { k: 'jellyfin_user', label: '用户名' },
        ],
      },
      {
        title: 'Plex',
        fields: [
          { k: 'plex_url', label: '地址' },
          { k: 'plex_token', label: 'Token', t: 'password' },
        ],
      },
      {
        title: '通用',
        fields: [
          { k: 'enable_auto_complete', label: '跳过已入库资源', t: 'bool',
            hint: '订阅前先查媒体库，已有的不再下载' },
        ],
      },
      {
        title: '媒体联动',
        hint: '刮削工具建立硬链接后登记关联，媒体服务器删除影片时同步清理种子与文件',
        panel: 'medialink',
        fields: [
          // Webhook 密钥不放在这里 —— 下方面板里那一份带「生成随机密钥」和
          // 明文仅一次可见的提示，两处都放会让人以为是两个不同的配置项
          { k: 'medialink_library_path', label: '媒体库根目录', ph: '/volume3/h_video',
            hint: '按 inode 反查硬链接的扫描范围。可设成上一层，把多个分类目录都纳入' },
          { k: 'medialink_scrape_dir', label: '刮削输出目录', ph: '/volume3/h_video/日本AV',
            hint: '刮削工具实际写入硬链接的目录。留空则等同库根。该目录受保护，不会被当成空目录清掉' },
          { k: 'medialink_delete_enabled', label: '启用联动删除', t: 'bool',
            hint: '删除不可逆。建议先用下方演练确认反查结果无误再开启' },
        ],
      },
      {
        title: '监控目录',
        hint: '源目录里的文件自动硬链接到目标目录。实时监听靠 inotify，NAS 与 Docker 绑定挂载上事件常丢，定时对账才是兜底',
        fields: [
          { k: 'watchdir_auto_sync', label: '自动同步', t: 'bool',
            hint: '关闭后定时对账与实时监听都不运行，只能在监控目录页手动同步' },
          { k: 'watchdir_sync_interval', label: '对账间隔（分钟）', t: 'number', ph: '30',
            hint: '实时监听收不到事件时，靠这个兜底。每轮要扫源目录并逐条比对，目录很大时别设太小' },
          { k: 'watchdir_delete_grace', label: '删除宽限期（秒）', t: 'number', ph: '1800',
            hint: '文件消失后先观察这么久再删，期间若同 inode 文件在别处出现则判定为移动。0 表示发现即删' },
        ],
      },
    ],
    tests: ['emby', 'jellyfin', 'plex'],
  },
  {
    key: 'subtitle',
    cat: 'library',
    title: '字幕',
    sections: [
      {
        title: '自动抓取',
        hint: '刮削登记完成后按番号找字幕，放到影片旁边。只认简体中文 —— '
          + '字幕站的「中文」大量是繁体或机翻日文，挑不出简体就不放文件。'
          + '关掉开关后仍可在番号详情页手动抓',
        fields: [
          { k: 'subtitle_enabled', label: '启用自动抓取', t: 'bool',
            hint: '入库即抓，并允许定时补漏' },
          { k: 'subtitle_fill_limit', label: '补漏每轮上限', t: 'number', ph: '30',
            hint: '每部要跨境请求两三次，媒体库大时别设太高' },
          { k: 'subtitle_fill_time', label: '补漏时间',
            hint: 'crontab 格式。默认凌晨跑，避开白天与抓取任务抢过盾服务' },
        ],
      },
    ],
  },
  {
    key: 'pt',
    cat: 'pipeline',
    title: 'PT 站点',
    sections: [
      {
        title: 'M-Team',
        fields: [
          { k: 'mteam_api_key', label: 'API Key', t: 'password' },
        ],
      },
      {
        title: 'Rousi',
        hint: '填了账号密码会自动登录续期，Token 可留空',
        fields: [
          { k: 'rousi_username', label: '用户名' },
          { k: 'rousi_password', label: '密码', t: 'password' },
          { k: 'rousi_token', label: 'Token', t: 'password',
            hint: '手动填时到期需自行更新' },
          { k: 'rousi_passkey', label: 'Passkey', t: 'password',
            hint: '取自 announce 地址中间那段' },
        ],
      },
      {
        title: 'PTTime',
        fields: [
          { k: 'ptt_cookie', label: 'Cookie', t: 'password' },
        ],
      },
      {
        title: 'NicePT',
        fields: [
          { k: 'nicept_cookie', label: 'Cookie', t: 'password' },
        ],
      },
    ],
    tests: ['mteam', 'rousi', 'ptt', 'nicept'],
  },
  {
    key: 'bt',
    cat: 'pipeline',
    title: '自定义 BT 源',
    sections: [
      {
        title: '请求配置',
        hint: '接入自建或第三方搜索接口',
        fields: [
          { k: 'bt_url', label: '请求地址', hint: '支持 ${keyword} 占位符' },
          { k: 'bt_method', label: '请求方法', t: 'select', options: ['get', 'post'] },
          { k: 'bt_header', label: '请求头 JSON', t: 'textarea' },
          { k: 'bt_json_data', label: '请求体 JSON', t: 'textarea' },         
          { k: 'bt_auto_download', label: '参与自动下载', t: 'bool',
            hint: '关掉后仍参与搜索、结果可手动下载，只是自动选种时不选它' },
        ],
      },
    ],
  },
  {
    key: 'notify',
    cat: 'assist',
    title: '通知',
    sections: [
      {
        title: 'Telegram',
        fields: [
          { k: 'telegram_bot_token', label: 'Bot Token', t: 'password' },
          { k: 'telegram_chat_id', label: 'Chat ID' },
          { k: 'telegram_whitelist', label: '白名单', hint: '多个用 | 分隔，留空则不限制' },
          { k: 'telegram_spoiler', label: '图片防剧透', t: 'bool' },
        ],
      },
      {
        title: '上行消息接收',
        hint: '要让 bot 收到消息，二选一',
        fields: [
          { k: 'telegram_receive_mode', label: '接收方式', t: 'select',
            options: ['webhook', 'polling'],
            hint: 'webhook 需公网 HTTPS；无公网地址选 polling' },
          { k: 'external_domain', label: '外网访问地址', ph: 'https://example.com',
            hint: '会自动补 /api/v1/message' },
        ],
        panel: 'telegram-receive',
      },
      {
        title: '消息订阅规则',
        hint: '收到消息后如何处理其中的番号',
        fields: [
          { k: 'msg_allow_prefixes', label: '番号前缀白名单', t: 'tags', upper: true,
            ph: '留空则不限制',
            hint: '只接受这些前缀；留空表示全部接受',
            suggestions: ['SSIS', 'NHDTB', 'JUL', 'ABP', 'IPX', 'MIDE', 'FC2'] },
          { k: 'msg_block_prefixes', label: '番号前缀黑名单', t: 'tags', upper: true,
            ph: '留空则不屏蔽',
            hint: '优先于白名单，命中即不订阅',
            suggestions: ['NHDTA', 'NHDTB', 'FC2'] },
          { k: 'msg_max_codes', label: '单条消息番号上限',
            hint: '超出部分不订阅并在回复中列出，0 表示不限' },
          { k: 'msg_auto_download', label: '订阅后立即检索', t: 'bool',
            hint: '开启后在后台搜种并推给下载器；关闭则等每日订阅下载任务' },
        ],
      },
      {
        title: '企业微信',
        fields: [
          { k: 'wechat_corp_id', label: 'CorpID' },
          { k: 'wechat_corp_secret', label: 'Secret', t: 'password' },
          { k: 'wechat_agent_id', label: 'AgentID' },
          { k: 'wechat_to_user', label: '推送用户' },
          { k: 'wechat_banner', label: '推送横幅图', t: 'bool' },
        ],
      },
    ],
    tests: ['telegram', 'wechat'],
  },
  {
    key: 'filter',
    cat: 'pipeline',
    title: '过滤与排序',
    sections: [
      {
        title: '种子筛选',
        hint: '不满足条件的种子直接排除，不会下载',
        fields: [
          { k: 'min_size', label: '最小体积', t: 'size', group: 'default_filter',
            ph: '如 5GB', hint: '留空不限' },
          { k: 'max_size', label: '最大体积', t: 'size', group: 'default_filter',
            ph: '如 10GB', hint: '留空不限' },
          { k: 'min_seeders', label: '最少做种数', group: 'default_filter',
            ph: '如 3', hint: '做种太少下不动，留空不限' },
          { k: 'max_seeders', label: '最多做种数', group: 'default_filter',
            ph: '留空不限', hint: '一般不用填' },
          { k: 'only_free', label: '只要免费种', t: 'bool', group: 'default_filter' },
          { k: 'only_chinese', label: '只要中文字幕', t: 'bool', group: 'default_filter' },
          { k: 'only_uc', label: '只要无码', t: 'bool', group: 'default_filter' },
          { k: 'exclude_uc', label: '排除无码', t: 'bool', group: 'default_filter' },
          { k: 'only_uhd', label: '只要 4K', t: 'bool', group: 'default_filter' },
          { k: 'exclude_uhd', label: '排除 4K', t: 'bool', group: 'default_filter' },
          { k: 'only_vr', label: '只要 VR', t: 'bool', group: 'default_filter' },
          { k: 'exclude_vr', label: '排除 VR', t: 'bool', group: 'default_filter',
            hint: 'VR 体积大且需专用播放器' },
          { k: 'include_keywords', label: '标题必含关键词', group: 'default_filter',
            hint: '逗号分隔，任一命中即通过' },
          { k: 'exclude_keywords', label: '标题排除关键词', group: 'default_filter',
            hint: '逗号分隔，任一命中即排除' },
        ],
      },
      {
        title: '优先级',
        hint: '多个种子都满足条件时选哪个',
        fields: [
          { k: 'primary_site', label: 'PT 主站', t: 'select',
            hint: '多站都有结果时优先选它，需排序规则里含 site' },
          { k: 'default_sort', label: '排序规则', t: 'sort' },
        ],
      },
      {
        title: '展示',
        fields: [
          { k: 'main_site', label: '主资源站', t: 'select', options: ['ALL', 'javdb', 'javbus'],
            hint: '番号信息与封面的来源' },
          { k: 'max_actor', label: '最大共演人数' },
          { k: 'image_mode', label: '图片模式', t: 'select',
            options: ['BLUR', 'VISIBLE', 'INVISIBLE'] },
          { k: 'enable_photo_cache', label: '图片持久化', t: 'bool' },
        ],
      },
    ],
  },
  {
    key: 'schedule',
    cat: 'system',
    title: '定时任务',
    sections: [
      {
        title: '订阅与下载',
        hint: 'crontab 格式，留空则不执行',
        fields: [
          { k: 'download_schedule_time', label: '订阅下载' },
          { k: 'actor_schedule_time', label: '演员订阅' },
          { k: 'rank_schedule_time', label: '榜单订阅' },
          { k: 'brand_schedule_time', label: '厂牌订阅' },
        ],
      },
      {
        title: '数据同步',
        hint: '只把番号收进库供浏览，不会订阅或下载',
        fields: [
          { k: 'sync_hot_time', label: '同步热门' },
          { k: 'sync_brands_time', label: '同步厂牌' },
          { k: 'sync_actors_time', label: '同步演员' },
          { k: 'sync_news', label: '同步新片' },
          { k: 'fill_empty_image_time', label: '补全缺图' },
          { k: 'brand_type', label: '同步哪些厂牌',
            hint: '留空则不同步；all 或逗号分隔，如 s1,moodyz' },
        ],
      },
      {
        title: '订阅范围',
        hint: '这里配置的会自动订阅并进入下载流程',
        fields: [
          { k: 'rank_page', label: '榜单订阅页数', hint: '0 表示不自动订阅' },
          { k: 'rank_type', label: '榜单类型', t: 'select',
            options: ['', 'daily', 'weekly', 'monthly'] },
          { k: 'brand_subscribe', label: '订阅哪些厂牌',
            hint: '留空则不订阅；all 或逗号分隔，如 s1,moodyz' },
          { k: 'brand_subscribe_days', label: '厂牌订阅天数',
            hint: '只订阅最近几天新发布的' },
        ],
      },
    ],
  },
  {
    key: 'auth',
    cat: 'system',
    title: '登录方式',
    sections: [
      {
        title: '单点登录 (OIDC)',
        hint: '接入 Authelia / Authentik / Keycloak 等身份提供商',
        fields: [
          { k: 'oidc_enabled', label: '启用此 OIDC 提供商', t: 'bool' },
          { k: 'oidc_display_name', label: '按钮文案', ph: 'SSO',
            hint: '登录页上「使用 XX 登录」的那个 XX' },
          { k: 'oidc_issuer', label: '提供商地址', ph: 'https://auth.example.com',
            hint: '用它拼 /.well-known/openid-configuration' },
          { k: 'oidc_client_id', label: '客户端 ID (Client ID)' },
          { k: 'oidc_client_secret', label: '客户端密钥 (Client Secret)', t: 'password' },
          { k: 'oidc_scope', label: '范围 (Scopes)', ph: 'openid profile email' },
        ],
        panel: 'oidc',
      },
      {
        title: 'Claim 映射',
        hint: '把 OIDC 返回的字段映射到本地账号',
        fields: [
          { k: 'oidc_username_claim', label: 'Username claim',
            ph: 'preferred_username' },
          { k: 'oidc_email_claim', label: '电子邮件 Claim', ph: 'email' },
          { k: 'oidc_name_claim', label: '显示名称 Claim', ph: 'name' },
          { k: 'oidc_bind_username', label: '绑定到本地账号',
            ph: '留空则用 Username claim 的值',
            hint: '单用户部署填 admin，所有 SSO 用户都登录到这个账号' },
        ],
      },
      {
        title: 'Passkey',
        hint: '指纹、面容或硬件密钥登录。需要 HTTPS',
        fields: [
          { k: 'webauthn_rp_id', label: 'RP ID', ph: '留空则自动推断',
            hint: '站点域名，不含端口与协议，如 example.com' },
          { k: 'webauthn_rp_name', label: '显示名称', ph: 'cinefold' },
        ],
        panel: 'passkey',
      },
    ],
  },
  {
    key: 'ai',
    cat: 'assist',
    title: '翻译与 AI',
    sections: [
      {
        title: '翻译',
        hint: '按 AI → 百度 → Google 的顺序取第一个配好的',
        fields: [
          { k: 'openai_url', label: 'AI 翻译接口' },
          { k: 'openai_model', label: 'AI 模型' },
          { k: 'openai_api_key', label: 'AI API Key', t: 'password' },
          { k: 'baidu_app_id', label: '百度翻译 AppID' },
          { k: 'baidu_api_key', label: '百度翻译 Key', t: 'password' },
          { k: 'google_api_key', label: 'Google 翻译 Key', t: 'password' },
        ],
      },
      {
        title: 'AI 助手',
        hint: '侧边悬浮的对话入口，可直接问系统当前情况。留空则沿用上面翻译的 AI 配置；'
          + '助手要做工具调用，建议用比翻译更强的模型',
        fields: [
          { k: 'agent_enabled', label: '启用助手', t: 'bool' },
          { k: 'agent_url', label: '接口地址', ph: 'https://api.openai.com/v1' },
          { k: 'agent_model', label: '模型', ph: 'gpt-4o-mini' },
          { k: 'agent_api_key', label: 'API Key', t: 'password' },
        ],
      },
    ],
  },
  {
    key: 'network',
    cat: 'system',
    title: '网络与存储',
    sections: [
      {
        title: '网络',
        fields: [
          { k: 'proxy', label: 'HTTP/SOCKS 代理', ph: 'socks5://127.0.0.1:7890' },
          { k: 'bypass_url', label: '反爬绕过服务', ph: 'http://127.0.0.1:8191/v1',
            hint: 'FlareSolverr 等，直连被拦时改走它' },
          { k: 'image_proxy_hosts', label: '图片代理域名',
            hint: '逗号分隔，追加到内置白名单' },
          { k: 'javdb_host', label: 'JavDB 地址',
            hint: '其余站点的地址在「数据源」页里改' },
        ],
      },
      {
        title: 'CloudDrive2',
        fields: [
          { k: 'cloudnas_url', label: '地址' },
          { k: 'cloudnas_username', label: '用户名' },
          { k: 'cloudnas_password', label: '密码', t: 'password' },
          { k: 'cloudnas_savepath', label: '保存路径' },
        ],
      },
    ],
  },
  {
    key: 'upgrade',
    cat: 'system',
    title: '版本与更新',
    sections: [
      {
        title: '版本与更新',
        hint: '更新包从 GitHub Releases 下载，装到 /data 挂载卷，重建容器不会丢',
        panel: 'upgrade',
        fields: [
          { k: 'github_token', label: 'GitHub Token', t: 'password',
            hint: '公开仓库可留空；配上能把 API 限额从 60 次/小时抬到 5000' },
          { k: 'github_proxy', label: 'GitHub 代理', ph: 'https://edgeone.gh-proxy.org/',
            hint: '直连 GitHub 不通时填，留空走直连' },
          { k: 'github_proxy_send_token', label: '代理携带 Token', t: 'bool',
            hint: '默认不发，代理是第三方机器。但私有仓库不带 Token 一律 404，那就必须打开' },
          { k: 'auto_update_enabled', label: '自动更新', t: 'bool',
            hint: '检测到带更新包的新版本时自动安装并重启。会打断正在跑的任务' },
          { k: 'update_check_interval', label: '检查间隔（分钟）',
            hint: '自动更新开启时生效，默认 360' },
        ],
      },
    ],
  },
]

/** 某个大类下的分组，供侧边导航渲染 */
function groupsOf(cat) {
  return GROUPS.filter((group) => group.cat === cat)
}

/** 分组内的全部字段，save 时按此收集 */
function groupFields(group) {
  return group.sections.flatMap((section) => section.fields)
}

async function load() {
  loading.value = true
  try {
    const data = await getConfig()
    Object.assign(form, data || {})
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    // 只提交当前分组，避免一次写入过多字段
    const group = GROUPS.find((item) => item.key === activeGroup.value)
    const payload = {}
    groupFields(group).forEach((field) => {
      if (field.group) {
        // 嵌套项整体提交，后端按 dict 覆盖
        payload[field.group] = form[field.group]
      } else {
        payload[field.k] = form[field.k]
      }
    })
    await saveConfig(payload)
    toast.success('配置已保存')
    // 已落库，明文不再需要留在页面上
    if (payload.medialink_webhook_token) freshWebhookToken.value = ''
    await configStore.load(true)
    if (activeGroup.value === 'notify') await loadTgReceive()
  } catch (err) {
    toast.error(err.message)
  } finally {
    saving.value = false
  }
}

/** Telegram 给的是秒级时间戳 */
function formatTime(seconds) {
  if (!seconds) return '时间未知'
  return new Date(seconds * 1000).toLocaleString()
}

async function loadPtSites() {
  try {
    const data = await listPtSites()
    // 空串排首位，表示不指定主站
    ptSiteOptions.value = ['', ...(data?.sites || [])]
  } catch {
    ptSiteOptions.value = ['']
  }
}

async function loadAuthExtras() {
  try {
    authOrigin.value = await getOidcRedirectUri()
    oidcRedirectUri.value = authOrigin.value.redirect_uri || ''
  } catch {
    authOrigin.value = null
    oidcRedirectUri.value = ''
  }
  try {
    const data = await listPasskeys()
    passkeys.value = data.items || []
  } catch {
    passkeys.value = []
  }
}

async function runOidcTest() {
  oidcBusy.value = true
  try {
    // 用输入框里的当前值探测，省得先保存
    oidcTest.value = await testOidc(form.oidc_issuer || '')
    if (oidcTest.value.success) toast.success('提供商连接成功')
    else toast.error(oidcTest.value.message)
  } catch (err) {
    oidcTest.value = { success: false, message: err.message }
    toast.error(err.message)
  } finally {
    oidcBusy.value = false
  }
}

function copyRedirectUri() {
  if (!oidcRedirectUri.value) return
  navigator.clipboard?.writeText(oidcRedirectUri.value)
    .then(() => toast.success('已复制回调地址'))
    .catch(() => toast.error('复制失败，请手动选择'))
}

async function addPasskey() {
  passkeyBusy.value = true
  try {
    const { options } = await passkeyRegisterBegin()
    const credential = await createCredential(options)
    await passkeyRegisterFinish(credential, passkeyLabel.value)
    toast.success('Passkey 已添加')
    passkeyLabel.value = ''
    await loadAuthExtras()
  } catch (err) {
    if (!(err instanceof PasskeyCancelled)) toast.error(err.message)
  } finally {
    passkeyBusy.value = false
  }
}

async function removePasskey(item) {
  if (!window.confirm(`删除「${item.label}」？删除后这把钥匙不能再用于登录。`)) return
  try {
    await deletePasskey(item.credential_id)
    toast.success('已删除')
    await loadAuthExtras()
  } catch (err) {
    toast.error(err.message)
  }
}

async function loadTgReceive() {
  try {
    tgReceive.value = await getTelegramReceive()
  } catch {
    // Token 没配或网络不通时不展示状态，不打扰用户
    tgReceive.value = null
  }
}

async function applyWebhook() {
  tgBusy.value = 'set'
  try {
    // 用输入框里的当前值，省得先保存再设置
    const data = await setTelegramWebhook(form.external_domain || '')
    if (data.success) toast.success(data.message)
    else toast.error(data.message)
    await loadTgReceive()
  } catch (err) {
    toast.error(err.message)
  } finally {
    tgBusy.value = ''
  }
}

async function removeWebhook() {
  tgBusy.value = 'del'
  try {
    const data = await deleteTelegramWebhook()
    if (data.success) toast.success(data.message)
    else toast.error(data.message)
    await loadTgReceive()
  } catch (err) {
    toast.error(err.message)
  } finally {
    tgBusy.value = ''
  }
}

async function test(target) {
  testing.value = target
  try {
    const data = await testConnection(target)
    testResults[target] = data
    if (data.success) toast.success(`${target}: ${data.message}`)
    else toast.error(`${target}: ${data.message}`)
  } catch (err) {
    testResults[target] = { success: false, message: err.message }
    toast.error(err.message)
  } finally {
    testing.value = ''
  }
}

// ---------------------------------------------------------------- 更新
const upgrade = ref(null)      // 安装情况：镜像版本 / overlay 版本 / 能否回退
const release = ref(null)      // 最新 release 的检测结果
const upgradeBusy = ref('')    // check / install / rollback
let upgradeTimer = null
// 重启期间接口会连不上，这一段不当成失败，只显示"重启中"
const restarting = ref(false)

const progress = computed(() => upgrade.value?.progress || null)
const upgradeRunning = computed(() => !!progress.value?.running)

async function loadUpgrade() {
  try {
    upgrade.value = await getUpgradeStatus()
    if (restarting.value) {
      // 能拿到响应说明后端起来了，刷新一次配置让版本号跟上
      restarting.value = false
      toast.success(`已更新到 ${upgrade.value?.running_version || '新版本'}`)
      await load()
      checkRelease()
    }
    if (!upgradeRunning.value) stopUpgradePoll()
  } catch {
    // 升级到重启这一步接口必然断一会儿，正在跑就继续等
    if (upgradeRunning.value || upgradeBusy.value === 'install' || upgradeBusy.value === 'rollback') {
      restarting.value = true
    }
  }
}

function startUpgradePoll() {
  stopUpgradePoll()
  upgradeTimer = setInterval(loadUpgrade, 2000)
}

function stopUpgradePoll() {
  if (upgradeTimer) {
    clearInterval(upgradeTimer)
    upgradeTimer = null
  }
  upgradeBusy.value = ''
}

async function checkRelease(refresh = false) {
  upgradeBusy.value = 'check'
  try {
    release.value = await checkVersion(refresh)
    if (refresh) {
      if (!release.value?.checked) toast.error(release.value?.error || '查询失败，检查网络或代理配置')
      else if (release.value.has_update) toast.success(`发现新版本 ${release.value.latest}`)
      else toast.success('已是最新版本')
    }
  } catch (err) {
    if (refresh) toast.error(err.message || '检查更新失败')
  } finally {
    upgradeBusy.value = ''
  }
}

async function doUpgrade() {
  if (!confirm(
    `将下载并安装 ${release.value?.latest}，安装完成后程序会自动重启，`
    + '正在运行的任务会被中断。继续？'
  )) return

  upgradeBusy.value = 'install'
  try {
    const res = await startUpgrade()
    toast.success(res?.message || '已开始升级')
    startUpgradePoll()
  } catch (err) {
    toast.error(err.message || '启动升级失败')
    upgradeBusy.value = ''
  }
}

async function doRollback() {
  const target = upgrade.value?.backup_version
    || `镜像版本 ${upgrade.value?.image_version || ''}`
  if (!confirm(`将回退到 ${target} 并重启程序。继续？`)) return

  upgradeBusy.value = 'rollback'
  try {
    const res = await rollbackUpgrade()
    toast.success(res?.message || '正在回退')
    restarting.value = true
    startUpgradePoll()
  } catch (err) {
    toast.error(err.message || '回退失败')
    upgradeBusy.value = ''
  }
}

onMounted(async () => {
  await load()
  loadPtSites()
  loadTgReceive()
  loadAuthExtras()
  loadUpgrade()
  checkRelease()
})

onUnmounted(stopUpgradePoll)
</script>

<template>
  <div class="gap-4 lg:flex lg:items-start">
    <!-- 分组导航。窄屏横向滚动一排，宽屏收成左侧竖栏 -->
    <nav
      class="-mx-1 mb-4 flex gap-4 overflow-x-auto px-1 pb-2 lg:mx-0 lg:mb-0 lg:w-44
             lg:shrink-0 lg:flex-col lg:gap-4 lg:overflow-visible lg:pb-0"
    >
      <div v-for="cat in CATEGORIES" :key="cat.key" class="shrink-0 lg:shrink">
        <p class="mb-1.5 px-1 text-[11px] font-medium uppercase tracking-wide text-gray-600">
          {{ cat.title }}
        </p>
        <div class="flex gap-2 lg:flex-col lg:gap-1">
          <button
            v-for="group in groupsOf(cat.key)"
            :key="group.key"
            class="btn whitespace-nowrap px-3 py-1.5 text-xs lg:text-left"
            :class="activeGroup === group.key ? 'bg-brand text-white' : 'btn-ghost'"
            @click="activeGroup = group.key"
          >
            {{ group.title }}
          </button>
        </div>
      </div>
    </nav>

    <div class="min-w-0 flex-1 space-y-4">
    <LoadingBlock v-if="loading" :rows="6" />

    <template v-else>
      <div v-for="group in GROUPS" v-show="activeGroup === group.key" :key="group.key" class="space-y-4">
        <div v-for="section in group.sections" :key="section.title" class="card space-y-3">
          <div>
            <p class="text-sm font-medium text-gray-300">{{ section.title }}</p>
            <p v-if="section.hint" class="mt-0.5 text-[11px] text-gray-600">
              {{ section.hint }}
            </p>
          </div>

          <div class="grid gap-4 sm:grid-cols-2">
            <ConfigField
              v-for="field in section.fields"
              :key="field.k"
              :field="field"
              :form="form"
              :options="field.k === 'primary_site' ? ptSiteOptions : null"
            />
          </div>

          <!-- 媒体联动：webhook 地址与接入说明 -->
          <template v-if="section.panel === 'medialink'">
            <!-- 删除不可逆，把风险和演练方式摆在最显眼处 -->
            <div
              v-if="form.medialink_delete_enabled"
              class="space-y-1 rounded border border-amber-900/60 bg-amber-950/30 p-2.5"
            >
              <p class="text-xs text-amber-400">联动删除已启用</p>
              <p class="text-[11px] text-gray-500">
                收到删除事件会直接删除种子与磁盘文件，不可恢复。接入新的媒体服务器
                前，建议先带 <code class="text-emerald-400">?dry_run=1</code>
                调用一次，确认反查结果无误
              </p>
            </div>

            <div
              v-if="!form.medialink_library_path"
              class="space-y-1 rounded border border-gray-800 bg-gray-900/40 p-2.5"
            >
              <p class="text-[11px] text-gray-500">
                未填媒体库根目录时无法按 inode 反查硬链接，刮削回调只会记录失败
              </p>
            </div>

            <div class="space-y-3 border-t border-gray-800 pt-3">
              <div class="space-y-1">
                <div class="flex items-center justify-between gap-2">
                  <p class="text-xs font-medium text-gray-400">Webhook 密钥</p>
                  <button
                    class="btn-ghost shrink-0 px-2 py-1 text-[11px]"
                    @click="generateWebhookToken"
                  >
                    生成随机密钥
                  </button>
                </div>

                <!-- 也允许手填。这里是密钥唯一的输入处 -->
                <input
                  v-model="form.medialink_webhook_token"
                  type="password"
                  class="input"
                  placeholder="留空则不校验，强烈建议设置"
                />

                <!-- 保存后接口只回传掩码，明文仅此一次可见 -->
                <div v-if="freshWebhookToken" class="space-y-1">
                  <div class="flex items-center gap-2">
                    <code class="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded bg-gray-900 px-2 py-1.5 font-mono text-[11px] text-emerald-400">
                      {{ freshWebhookToken }}
                    </code>
                    <button
                      class="btn-ghost shrink-0 px-2 py-1 text-[11px]"
                      @click="copyText(freshWebhookToken, '密钥')"
                    >
                      复制
                    </button>
                  </div>
                  <p class="text-[11px] text-amber-400">
                    尚未保存。请先复制留存 —— 保存后只显示掩码，无法再查看明文
                  </p>
                </div>
                <p v-else class="text-[11px] text-gray-600">
                  已保存的密钥只显示掩码。忘记了就重新生成一个，并同步更新
                  刮削工具与媒体服务器的配置
                </p>
              </div>

              <div v-for="hook in medialinkHooks" :key="hook.key" class="space-y-1 border-t border-gray-800 pt-3">
                <p class="text-xs font-medium text-gray-400">{{ hook.title }}</p>
                <div class="flex items-center gap-2">
                  <code class="min-w-0 flex-1 truncate rounded bg-gray-900 px-2 py-1.5 font-mono text-[11px] text-emerald-400">
                    {{ hook.url }}
                  </code>
                  <button
                    class="btn-ghost shrink-0 px-2 py-1 text-[11px]"
                    @click="copyText(hook.url, '回调地址')"
                  >
                    复制
                  </button>
                </div>
                <p class="text-[11px] text-gray-600">{{ hook.hint }}</p>
              </div>

              <div class="space-y-1 border-t border-gray-800 pt-3">
                <p class="text-xs font-medium text-gray-400">刮削工具 Body 模板</p>
                <div class="flex items-center gap-2">
                  <code class="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded bg-gray-900 px-2 py-1.5 font-mono text-[11px] text-gray-300">
                    {{ SCRAPE_BODY_TEMPLATE }}
                  </code>
                  <button
                    class="btn-ghost shrink-0 px-2 py-1 text-[11px]"
                    @click="copyText(SCRAPE_BODY_TEMPLATE, 'Body 模板')"
                  >
                    复制
                  </button>
                </div>
                <p class="text-[11px] text-gray-600">
                  Windows 路径的反斜杠无需手动转义，接收端会自动处理
                </p>
              </div>

              <p
                v-if="form.medialink_webhook_token"
                class="text-[11px] text-gray-600"
              >
                调用时需附带请求头
                <code class="text-emerald-400">X-Cinefold-Token</code>
                ，值为上面填的密钥
              </p>
            </div>
          </template>

          <!-- OIDC 回调地址与连接测试 -->
          <template v-if="section.panel === 'oidc'">
            <!-- 地址算错是这两个功能最常见的失败原因，先把它摆明 -->
            <div
              v-if="authOrigin && originMismatch"
              class="space-y-1 rounded border border-amber-900/60 bg-amber-950/30 p-2.5"
            >
              <p class="text-xs text-amber-400">服务端识别的地址与当前浏览器不一致</p>
              <p class="font-mono text-[11px] text-gray-400">
                服务端：{{ authOrigin.origin }}
              </p>
              <p class="font-mono text-[11px] text-gray-400">
                浏览器：{{ browserOrigin }}
              </p>
              <p class="text-[11px] text-gray-500">
                SSO 与 Passkey 都会因此失败。请在「其他 → 外网访问地址」填写
                <code class="text-emerald-400">{{ browserOrigin }}</code> 后保存
              </p>
            </div>

            <div class="space-y-2 border-t border-gray-800 pt-3">
              <p class="text-xs font-medium text-gray-400">回调重定向 URI</p>
              <div class="flex items-center gap-2">
                <code class="min-w-0 flex-1 truncate rounded bg-gray-900 px-2 py-1.5 font-mono text-[11px] text-emerald-400">
                  {{ oidcRedirectUri || '保存外网地址后显示' }}
                </code>
                <button
                  class="btn-ghost shrink-0 px-2 py-1 text-[11px]"
                  :disabled="!oidcRedirectUri"
                  @click="copyRedirectUri"
                >
                  复制
                </button>
              </div>
              <p class="text-[11px] text-gray-600">
                把这个地址加到提供商的允许回调 URL 列表里
              </p>

              <div class="flex flex-wrap items-center gap-2 pt-1">
                <button
                  class="btn-ghost px-3 py-1.5 text-xs"
                  :disabled="oidcBusy"
                  @click="runOidcTest"
                >
                  {{ oidcBusy ? '探测中…' : '测试提供商' }}
                </button>
                <span
                  v-if="oidcTest"
                  class="text-xs"
                  :class="oidcTest.success ? 'text-emerald-400' : 'text-red-400'"
                >
                  {{ oidcTest.message }}
                </span>
              </div>

              <div v-if="oidcTest?.success" class="space-y-0.5 pt-1">
                <p class="font-mono text-[11px] text-gray-600">
                  authorize: {{ oidcTest.authorization_endpoint }}
                </p>
                <p class="font-mono text-[11px] text-gray-600">
                  token: {{ oidcTest.token_endpoint }}
                </p>
              </div>
            </div>
          </template>

          <!-- Passkey 管理 -->
          <template v-if="section.panel === 'passkey'">
            <div class="space-y-3 border-t border-gray-800 pt-3">
              <p v-if="!passkeySupported" class="text-xs text-amber-400">
                当前浏览器或环境不支持 Passkey，需要 HTTPS
              </p>

              <template v-else>
                <p v-if="authOrigin" class="text-[11px] text-gray-600">
                  当前生效的 RP ID：
                  <code class="text-gray-400">{{ authOrigin.rp_id || '—' }}</code>
                  ，校验 origin：
                  <code :class="originMismatch ? 'text-amber-400' : 'text-gray-400'">
                    {{ authOrigin.origin }}
                  </code>
                </p>

                <div class="flex flex-wrap items-center gap-2">
                  <input
                    v-model="passkeyLabel"
                    class="input w-44"
                    placeholder="给这把钥匙起个名"
                  />
                  <button
                    class="btn-ghost px-3 py-1.5 text-xs"
                    :disabled="passkeyBusy"
                    @click="addPasskey"
                  >
                    {{ passkeyBusy ? '等待验证…' : '添加 Passkey' }}
                  </button>
                </div>

                <div v-if="passkeys.length" class="space-y-1">
                  <div
                    v-for="item in passkeys"
                    :key="item.credential_id"
                    class="flex items-center gap-2 text-xs"
                  >
                    <span class="text-gray-200">{{ item.label }}</span>
                    <span class="text-[11px] text-gray-600">
                      {{ item.last_used ? `最近使用 ${item.last_used}` : '未使用过' }}
                    </span>
                    <button
                      class="ml-auto text-[11px] text-red-400 hover:underline"
                      @click="removePasskey(item)"
                    >
                      删除
                    </button>
                  </div>
                </div>
                <p v-else class="text-xs text-gray-600">还没有注册任何 Passkey</p>
              </template>
            </div>
          </template>

          <!-- Telegram 上行消息接收状态与操作 -->
          <template v-if="section.panel === 'telegram-receive'">
            <div class="flex items-center justify-between border-t border-gray-800 pt-3">
              <p class="text-xs font-medium text-gray-400">当前状态</p>
              <button class="btn-ghost px-2 py-1 text-[11px]" @click="loadTgReceive">
                刷新状态
              </button>
            </div>

            <div v-if="tgReceive" class="space-y-1 text-xs text-gray-400">
              <p>
                当前方式：<span class="text-gray-200">{{ tgReceive.mode }}</span>
                <span
                  v-if="tgReceive.mode === 'polling'"
                  :class="tgReceive.polling_running ? 'text-emerald-400' : 'text-amber-400'"
                >
                  （{{ tgReceive.polling_running ? '轮询运行中' : '未运行，保存配置后生效' }}）
                </span>
              </p>
              <p>
                Webhook：
                <span :class="tgReceive.webhook_url ? 'text-emerald-400' : 'text-gray-500'">
                  {{ tgReceive.webhook_url || '未设置' }}
                </span>
              </p>
              <p v-if="tgReceive.pending_update_count" class="text-amber-400">
                有 {{ tgReceive.pending_update_count }} 条消息堆积未处理
              </p>
              <p v-if="tgReceive.last_error_message" class="text-amber-400">
                最近回调错误（{{ formatTime(tgReceive.last_error_date) }}）：
                {{ tgReceive.last_error_message }}
                <span class="text-gray-600">
                  — Telegram 会保留到下次回调成功为止，修好后发条消息即可刷新
                </span>
              </p>
            </div>
            <p v-else class="text-xs text-gray-600">填好 Bot Token 并保存后可查看状态</p>

            <div class="flex flex-wrap gap-2">
              <button
                class="btn-ghost px-3 py-1.5 text-xs"
                :disabled="tgBusy === 'set'"
                @click="applyWebhook"
              >
                {{ tgBusy === 'set' ? '设置中…' : '设置 Webhook' }}
              </button>
              <button
                class="btn-ghost px-3 py-1.5 text-xs"
                :disabled="tgBusy === 'del'"
                @click="removeWebhook"
              >
                {{ tgBusy === 'del' ? '删除中…' : '删除 Webhook' }}
              </button>
            </div>
            <p class="text-[11px] text-gray-600">
              Webhook 需公网 HTTPS，端口限 443/80/88/8443；两种方式互斥，
              切到 polling 会自动删除 webhook
            </p>
          </template>

          <!-- 版本与更新 -->
          <template v-if="section.panel === 'upgrade'">
            <div class="flex items-center justify-between border-t border-gray-800 pt-3">
              <p class="text-xs font-medium text-gray-400">版本</p>
              <button
                class="btn-ghost px-2 py-1 text-[11px]"
                :disabled="upgradeBusy === 'check' || upgradeRunning"
                @click="checkRelease(true)"
              >
                {{ upgradeBusy === 'check' ? '检查中…' : '检查更新' }}
              </button>
            </div>

            <div class="space-y-1 text-xs text-gray-400">
              <p>
                当前运行：<span class="text-gray-200">{{ upgrade?.running_version || '—' }}</span>
                <span v-if="upgrade?.overlay_version" class="ml-1 text-gray-600">
                  （热更新装的，镜像自带 {{ upgrade.image_version }}）
                </span>
              </p>
              <p v-if="release?.checked">
                最新版本：<span class="text-gray-200">{{ release.latest }}</span>
                <span v-if="!release.has_update" class="ml-1 text-emerald-400">已是最新</span>
                <span v-else-if="!release.can_upgrade" class="ml-1 text-amber-400">
                  该版本未提供更新包，需在宿主机执行 docker compose pull
                </span>
              </p>
              <p v-else-if="release" class="text-gray-600">
                {{ release.error || '查不到最新版本，检查网络或代理配置' }}
              </p>
              <p v-if="upgrade?.backup_version" class="text-gray-600">
                可回退到 {{ upgrade.backup_version }}
              </p>
            </div>

            <!-- 更新说明 -->
            <details v-if="release?.has_update && release.notes" class="text-xs">
              <summary class="cursor-pointer text-gray-500 hover:text-gray-300">
                更新说明
              </summary>
              <pre class="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-gray-950 p-2 text-[11px] text-gray-400">{{ release.notes }}</pre>
            </details>

            <!-- 进度 -->
            <div v-if="upgradeRunning || restarting || progress?.stage === 'failed'" class="space-y-1.5">
              <div class="h-1.5 overflow-hidden rounded-full bg-gray-800">
                <div
                  class="h-full transition-all duration-300"
                  :class="progress?.stage === 'failed' ? 'bg-red-500' : 'bg-brand'"
                  :style="{ width: `${restarting ? 98 : progress?.percent || 0}%` }"
                />
              </div>
              <p
                class="text-xs"
                :class="progress?.stage === 'failed' ? 'text-red-400' : 'text-gray-400'"
              >
                {{ restarting ? '程序重启中，稍候会自动刷新…' : progress?.message || '准备中…' }}
              </p>
              <details v-if="progress?.logs?.length" class="text-[11px]">
                <summary class="cursor-pointer text-gray-600 hover:text-gray-400">
                  详细日志
                </summary>
                <pre class="mt-1 max-h-40 overflow-auto rounded bg-gray-950 p-2 text-gray-500">{{ progress.logs.join('\n') }}</pre>
              </details>
            </div>

            <div class="flex flex-wrap gap-2">
              <button
                v-if="release?.has_update && release.can_upgrade"
                class="btn bg-brand px-3 py-1.5 text-xs text-white"
                :disabled="upgradeBusy === 'install' || upgradeRunning || restarting"
                @click="doUpgrade"
              >
                {{ upgradeRunning || restarting ? '升级中…' : `更新到 ${release.latest}` }}
              </button>
              <button
                v-if="upgrade?.can_rollback"
                class="btn-ghost px-3 py-1.5 text-xs"
                :disabled="upgradeBusy === 'rollback' || upgradeRunning || restarting"
                @click="doRollback"
              >
                {{ upgradeBusy === 'rollback' ? '回退中…' : '回退上一版' }}
              </button>
            </div>

            <p class="text-[11px] text-gray-600">
              更新包解压到 {{ upgrade?.update_dir || '/data/updates' }}，
              装完自动重启。装出问题可以回退，回退后仍有问题就在宿主机跑
              docker compose pull 回到镜像版本
            </p>
          </template>
        </div>

        <!-- 连接测试 -->
        <div v-if="group.tests" class="card space-y-2">
          <p class="text-sm font-medium text-gray-300">连接测试</p>
          <div class="flex flex-wrap gap-2">
            <button
              v-for="target in group.tests"
              :key="target"
              class="btn-ghost px-3 py-1.5 text-xs"
              :disabled="testing === target"
              @click="test(target)"
            >
              {{ testing === target ? '测试中…' : target }}
            </button>
          </div>
          <div v-for="(result, target) in testResults" :key="target" class="text-xs">
            <span :class="result.success ? 'text-emerald-400' : 'text-red-400'">
              {{ target }}: {{ result.message }}
            </span>
          </div>
        </div>

        <div class="flex gap-2">
          <button class="btn-primary" :disabled="saving" @click="save">
            {{ saving ? '保存中…' : `保存「${group.title}」` }}
          </button>
          <button class="btn-ghost" @click="load">重新加载</button>
        </div>
      </div>

      <p class="text-xs text-gray-600">
        密码类字段显示为掩码时表示未改动，直接保存不会覆盖原值
      </p>
    </template>
    </div>
  </div>
</template>
