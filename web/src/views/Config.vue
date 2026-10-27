<script setup>
import { useVisibleTimers } from '@/composables/useVisibleTimers'
const { setTimeout, clearTimeout, setInterval, clearInterval } = useVisibleTimers()
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  checkVersion, deletePasskey, deleteTelegramWebhook, getConfig, getOidcRedirectUri,
  getTelegramReceive, getUpgradeStatus, listPasskeys, listPtSites, passkeyRegisterBegin,
  passkeyRegisterFinish, rollbackUpgrade, saveConfig, setTelegramWebhook, startUpgrade,
  testConnection, testOidc,
} from '@/api'
import { PasskeyCancelled, createCredential, isSupported } from '@/utils/webauthn'
import { useRoute } from 'vue-router'
import { useToast } from '@/composables/useToast'
import { useConfigStore } from '@/stores/config'
import ConfigField from '@/components/ConfigField.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import WatermarkPreview from '@/components/WatermarkPreview.vue'
import MihomoPanel from '@/components/MihomoPanel.vue'

const toast = useToast()
const configStore = useConfigStore()

const form = reactive({})
const loading = ref(true)
const saving = ref(false)
const route = useRoute()
const activeGroup = ref('downloader')
const scrapeTab = ref('basic')
const SCRAPE_TABS = [['basic', '目录与命名'], ['metadata', '元数据来源'], ['images', '图片与水印'], ['automation', '自动任务']]
const testing = ref('')
const testResults = reactive({})
const ptSiteDisabled = target => ['mteam', 'rousi', 'ptt', 'nicept'].includes(target) && form[`${target}_enabled`] === false

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
    title: 'Emby 事件回调',
    url: `${medialinkBase.value}/api/v1/webhook/emby`,
    hint: '删除联动选择 item.remove。开启播放记录后，还可选择 playback.start/stop/pause/unpause/progress，保留原始 Event、Item、User、Device/Session、PlayState 字段',
  },
])

// MDCng 的 Body 模板。反斜杠不转义也能解析，无需用户处理
// 每家翻译是否配好。判据必须与后端 translate.get_translators() 一致 ——
// 这里说「使用中」而后端实际没启用，比不显示状态更误导人
const TRANSLATE_PROVIDERS = [
  { key: 'ai', label: 'AI', need: ['openai_url', 'openai_api_key'] },
  { key: 'tencent', label: '腾讯', need: ['tencent_secret_id', 'tencent_secret_key'] },
  { key: 'baidu', label: '百度', need: ['baidu_app_id', 'baidu_api_key'] },
  { key: 'google', label: 'Google', need: ['google_api_key'] },
]

// 密码字段回显是掩码（一串 *），那也算「已配置」—— 它代表库里存着值，
// 只是没明文发给前端
function filled(key) {
  return Boolean(String(form[key] ?? '').trim())
}

const translateStatus = computed(() =>
  Object.fromEntries(
    TRANSLATE_PROVIDERS.map((p) => [p.key, p.need.every(filled)]),
  ),
)

// 降级链概览：实际会按哪个顺序尝试
const translateChain = computed(() =>
  TRANSLATE_PROVIDERS.filter((p) => translateStatus.value[p.key]),
)

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
          { k: 'emby_user_id', label: '观看统计用户 ID', hint: '媒体库页面可选择用户；同步其观看进度与播放次数' },
          { k: 'playback_tracking_enabled', label: '持续记录播放与日历', t: 'bool', hint: '每分钟只读 Emby 会话，不需要播放报告插件；从开启后记录，留空用户 ID 时记录所有用户。短于一分钟的播放可能漏记，可额外配置播放 Webhook 补充' },
          { k: 'playback_reporting_enabled', label: '优先读取 Playback Reporting 历史', t: 'bool', hint: '已安装插件时读取历史播放，无需重新累计；需要有管理员权限的 Emby API Key。插件不可用时显示本地观测记录和原因' },
          { k: 'playback_timezone', label: '播放日历时区', ph: 'Asia/Shanghai', hint: '按此时区分配每天的观看时间，跨午夜自动拆分' },
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
        title: '极影视（可选）',
        fields: [
          { k: 'zspace_url', label: '地址' },
          { k: 'zspace_cookie', label: 'Cookie', t: 'password' },
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
    key: 'scrape',
    cat: 'library',
    title: '刮削',
    sections: [
      {
        title: '刮削方式',
        tab: 'basic',
        hint: '选择由 cinefold 或外部工具负责自动刮削。两种模式都支持查看已保存的源视频清单。切换工具前，请先完成单片验证。',
        fields: [
          { k: 'scrape_mode', label: '由谁刮削', t: 'select',
            options: ['external', 'builtin'],
            optionLabels: { external: '外部工具（MDCng 等）', builtin: 'cinefold 内置刮削' },
            hint: 'external：等 MDCng 等外部工具回调 /webhook/scrape，行为与旧版一致。'
              + 'builtin：自己抓元数据、写 NFO、出图、建硬链接，不需要外部工具' },
        ],
      },
      {
        title: '重复影片处理',
        tab: 'basic',
        fields: [
          { k: 'scrape_duplicate_policy', label: '重复影片处理', t: 'select', options: ['versions', 'overwrite', 'highest_quality', 'keep_existing'],
            optionLabels: { versions: '保留两个版本', overwrite: '覆盖已有视频', highest_quality: '保留高清版本', keep_existing: '保留旧的，跳过本次文件' },
            hint: '手动、自动和重建刮削统一生效。完全相同的内容自动复用；不同字幕、破解版本及分集分别保留。高清按实际分辨率、码率比较，无法检测或片长明显不同时保留两个版本。覆盖只替换播放文件，下载源保留；保留旧的会记录处理结果，避免重复失败。' },
        ],
      },
      {
        title: '源目录与输出目录',
        tab: 'basic',
        hint: '目录配置统一在此管理；刮削工作台负责查询与执行任务。',
        fields: [
          { k: 'scrape_source_dirs', label: '下载源目录', t: 'textarea', hint: '多个目录用逗号分隔或每行一个。保存后首次进入工作台会后台建立清单，以后读取数据库；也可主动扫描更新。自动刮削只处理已稳定的视频，请确保下载完成后再放入。源目录与输出目录不能重叠' },
          { k: 'medialink_scrape_dir', label: '刮削输出目录', ph: '/volume3/h_video/日本AV', hint: '产物硬链接、NFO 与图片存放位置；留空使用媒体库根目录' },
        ],
      },
      {
        title: '产物命名',
        tab: 'basic',
        hint: '仅自建刮削生效。支持基础语法 {number} 与 Jinja2 高级语法 '
          + '{{ number }}，与 MDCng 的模板写法一致，配置可直接搬过来。'
          + '基础语法缺失字段填「未知」，高级语法填空并支持条件与 filter。'
          + '改模板前建议先在刮削页试算，确认产物路径符合预期',
        fields: [
          { k: 'scrape_dir_template', label: '目录模板', ph: '{category}/{first_actor}',
            hint: '相对「刮削输出目录」，留空表示不分子目录、全部平铺。'
              + '注意用 first_actor 而非 actor —— 后者是全部演员的逗号拼接，'
              + '多演员时会得到一个超长目录名' },
          { k: 'scrape_file_template', label: '文件名模板', ph: '{number}',
            hint: '不含扩展名。分集后缀（-CD1）会自动补，除非模板里已写了 {cd}' },
          { k: 'scrape_category', label: '分类名', ph: '日本AV',
            hint: '填进模板的 {category}，用来把不同类型的片分到不同目录' },
        ],
      },
      {
        title: '自动任务',
        tab: 'automation',
        hint: '视频清单按间隔后台更新，external 模式也生效。自动刮削仅在 builtin 模式运行。',
        fields: [
          { k: 'scrape_scan_interval', label: '目录核对间隔（分钟）', ph: '10' },
          { k: 'scrape_scan_limit', label: '每轮最多自动刮削番号数', ph: '10' },
          { k: 'scrape_overwrite', label: '默认覆盖 NFO/图片', t: 'bool', hint: '用于自动刮削与路径临时任务；视频列表中的手动任务可单独选择本次覆盖方式' },
          { k: 'scrape_notify', label: '入库完成通知', t: 'bool', hint: '使用已配置的通知渠道；完整登记成功后发送' },
        ],
      },
      {
        title: '抓取等待',
        tab: 'metadata',
        fields: [
          { k: 'scrape_metadata_timeout', label: '元数据总等待上限（秒）', ph: '25', hint: '翻译、下载图片和写文件另计' },
          { k: 'scrape_merge_grace', label: '首次命中后的补全等待（秒）', ph: '3', hint: '调大可等待首选源补全，调小可更快返回' },
        ],
      },
      {
        title: '来源优先级（按番号类型）',
        tab: 'metadata',
        hint: '从左到右优先；箭头可调整顺序，其余源按数据源页面排序补充。仅已启用、匹配番号规则的详情源参与。JavLibrary 当前仅用于榜单，FreeJavBT 尚未接入详情；欧美无番号影片暂不支持自动识别',
        fields: [['censored', '有码'], ['uncensored', '无码'], ['amateur', '素人'], ['fc2', 'FC2'], ['domestic', '国产']].map(([k, label]) => ({ k, label, group: 'scrape_source_priority', t: 'sources' })),
      },
      {
        title: '来源优先级（按字段）',
        collapsible: true,
        tab: 'metadata',
        hint: '字段首选无数据时按类型优先级补齐；未配置的字段沿用质量合并规则。已有资料仍只补空缺',
        fields: [['title', '标题 / 原标题'], ['cn_title', '中文标题'], ['outline', '简介'], ['casts', '演员'], ['banner', '封面'], ['poster', '海报'], ['still_photo', '剧照'], ['genres', '标签'], ['star', '评分'], ['release_date', '发行日期'], ['duration', '时长'], ['publisher', '发行商'], ['producer', '片商'], ['series', '系列']].map(([k, label]) => ({ k, label, group: 'scrape_field_priority', t: 'sources' })),
      },
      {
        title: '屏蔽来源（按字段）',
        collapsible: true,
        tab: 'metadata',
        hint: '忽略指定源新返回的对应字段；保留数据库已有值',
        fields: [['title', '标题 / 原标题'], ['cn_title', '中文标题'], ['outline', '简介'], ['casts', '演员'], ['banner', '封面'], ['poster', '海报'], ['still_photo', '剧照'], ['genres', '标签'], ['star', '评分']].map(([k, label]) => ({ k, label, group: 'scrape_field_exclude', t: 'sources' })),
      },
      {
        title: '图片水印',
        tab: 'images',
        panel: 'watermark',
        hint: '按明确的文件标记添加：破解、流出、无码、有码逐级覆盖。清晰度依据文件名中的 4K/8K/2160p/4320p；已有图片需开启重刮覆盖才改变',
        fields: [
          { k: 'enabled', group: 'scrape_watermark', label: '启用水印', t: 'bool' },
          { k: 'targets', group: 'scrape_watermark', label: '作用图片', t: 'tags', suggestions: ['poster', 'thumb', 'fanart'] },
          { k: 'types', group: 'scrape_watermark', label: '标记类型', t: 'tags', suggestions: ['sub', 'crack', 'leak', 'uncensored', 'censored', 'resolution'], hint: 'sub 字幕 / crack 破解 / leak 流出 / uncensored 无码 / censored 有码 / resolution 清晰度' },
          { k: 'layout', group: 'scrape_watermark', label: '布局', t: 'select', options: ['stack', 'clockwise', 'counterclockwise'], hint: 'stack 堆叠 / clockwise 顺时针 / counterclockwise 逆时针' },
          { k: 'corner', group: 'scrape_watermark', label: '起始位置', t: 'select', options: ['top_left', 'top_right', 'bottom_right', 'bottom_left'] },
          { k: 'scale', group: 'scrape_watermark', label: '大小（短边比例）', hint: '0.02～0.2，默认 0.07' },
          { k: 'offset_x', group: 'scrape_watermark', label: '横向边距比例', hint: '0～0.2' },
          { k: 'offset_y', group: 'scrape_watermark', label: '纵向边距比例', hint: '0～0.2' },
          { k: 'gap', group: 'scrape_watermark', label: '标记间距比例', hint: '0～0.1' },
          ...[['sub', '字幕'], ['crack', '破解'], ['leak', '流出'], ['uncensored', '无码'], ['censored', '有码'], ['resolution', '4K / 8K']].map(([k, label]) => ({ k: `position_${k}`, group: 'scrape_watermark', label: `${label}固定位置`, t: 'select', options: ['auto', 'top_left', 'top_right', 'bottom_right', 'bottom_left'] })),
        ],
      },
      {
        title: '图片输出',
        tab: 'images',
        fields: [
          { k: 'scrape_still_limit', label: '剧照张数', t: 'number', ph: '0',
            hint: '落进影片旁的 extrafanart 目录。每张都要跨境下载，0 表示不下' },
          { k: 'scrape_require_images', label: '严格校验图片', t: 'bool', hint: '主图或已请求的剧照下载、写入失败时不标记完成' },
          { k: 'scrape_poster_crop', label: '海报裁切', t: 'select', options: ['auto', 'always', 'never'], hint: 'auto 仅普通有码自动裁切；always 总是裁切宽图；never 保留完整图片' },
        ],
      },
    ],
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
      {
        title: '本地字幕库',
        hint: '手工下载来的字幕丢进一个目录，按番号自动认领。命名不用讲究 —— '
          + 'SSNI-497（迅雷）.srt、SONE-895 (1).ass 这类带来源噪声的都认，'
          + '也会递归扫子目录。优先级高于网络源：本地命中就不必跨境请求。'
          + '文件只读不删，可以反复用',
        fields: [
          { k: 'subtitle_local_dir', label: '字幕库目录', ph: '/subs',
            hint: '容器内的路径，记得先把宿主目录挂进容器。留空表示不启用' },
        ],
      },
    ],
  },
  {
    key: 'review',
    cat: 'library',
    title: 'AI 影评',
    sections: [
      {
        title: '自动生成',
        hint: '按番号的类别标签、演员与厂牌生成看点，写进影片旁的 NFO 与 '
          + 'Emby 简介。AI 没看过影片，只依据这些元数据以及同演员/同厂牌'
          + '历史作品的高频标签来归纳 —— 依据不足的字段留空，不编。'
          + '用哪套 AI 见下面的选项',
        fields: [
          { k: 'review_enabled', label: '启用自动生成', t: 'bool',
            hint: '刮削入库即生成，并允许定时补漏' },
          { k: 'review_provider', label: '用哪套 AI', t: 'select',
            options: ['auto', 'agent', 'translate'],
            hint: 'auto 先用助手、没配则回退翻译；agent / translate 指定死。'
              + '要生成结构化结果，模型太弱容易吐不出合法 JSON，'
              + '翻译那套若配的是小模型，建议选 agent' },
          { k: 'review_fill_limit', label: '补漏每轮上限', t: 'number', ph: '20',
            hint: '每部一次 AI 请求，媒体库大时别设太高' },
          { k: 'review_fill_time', label: '补漏时间',
            hint: 'crontab 格式。默认排在补抓字幕之后，两个任务错开跑' },
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
          { k: 'mteam_enabled', label: '启用此站点', t: 'bool', hint: '停用后保留凭据，暂停搜索与新下载' },
          { k: 'mteam_api_key', label: 'API Key', t: 'password' },
        ],
      },
      {
        title: 'Rousi',
        hint: '只需 API Key，不会过期也无需续期',
        fields: [
          { k: 'rousi_enabled', label: '启用此站点', t: 'bool', hint: '停用后保留凭据，暂停搜索与新下载' },
          { k: 'rousi_apikey', label: 'API Key', t: 'password',
            hint: '站点「个人 API Key」页签发，需授予「读取账户资料」「读取与搜索种子」「下载种子」三项权限' },
        ],
      },
      {
        title: 'PTTime',
        fields: [
          { k: 'ptt_enabled', label: '启用此站点', t: 'bool', hint: '默认停用；确认站点可访问后可重新启用，原 Cookie 保留' },
          { k: 'ptt_cookie', label: 'Cookie', t: 'password' },
        ],
      },
      {
        title: 'NicePT',
        fields: [
          { k: 'nicept_enabled', label: '启用此站点', t: 'bool', hint: '默认停用；确认站点可访问后可重新启用，原 Cookie 保留' },
          { k: 'nicept_cookie', label: 'Cookie', t: 'password' },
        ],
      },
    ],
    tests: ['mteam', 'rousi', 'ptt', 'nicept'],
  },
  {
    key: 'resources',
    cat: 'pipeline',
    title: '资源库与爬虫',
    sections: [
      {
        title: '原生资源爬虫',
        hint: '论坛磁链直接写入本程序资源库，不依赖 article 或旧库导入。地址、Cookie、代理在数据源页面设置。',
        fields: [
          { k: 'resource_catalog_enabled', label: '搜索包含本地资源库', t: 'bool', hint: '搜索时包含原生爬虫采集到的磁链' },
          { k: 'bt_auto_download', label: '本地资源参与自动下载', t: 'bool', hint: '关闭后仍可搜索和手动下载' },
          { k: 'resource_crawl_time', label: '资源抓取时间', hint: 'crontab 格式，留空不自动执行；任务页可手动运行' },
          { k: 'resource_metadata_time', label: '影片与演员抓取时间', hint: 'Avbase 新片、JavDB 榜单、JavLibrary 榜单及演员；留空不自动执行' },
          { k: 'resource_crawl_pages', label: '每板块最多页数', t: 'number', hint: '1–100，默认 2' },
          { k: 'resource_crawl_limit', label: '每轮帖子上限', t: 'number', hint: '1–1000，默认 50，可继续未完成的帖子' },
          { k: 'resource_sht_sections', label: '色花堂板块', hint: '逗号分隔，默认 37,103,104' },
          { k: 'resource_x1080_sections', label: 'X1080X 板块', hint: '逗号分隔，默认 5206,5479,5208' },
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
      {
        title: 'Discord',
        hint: '在 Discord 频道的整合设置中创建 Webhook；保存后自动接收系统通知。',
        fields: [{ k: 'discord_webhook_url', label: 'Webhook URL', t: 'password' }],
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
          { k: 'enable_photo_cache', label: '提前缓存封面', t: 'bool', hint: '关闭后仍会按需缓存浏览过的图片' },
          { k: 'photo_cache_scope', label: '提前缓存范围', t: 'select', options: ['important', 'all'], hint: 'important 只缓存订阅、入库与收藏影片；all 缓存全部情报' },
          { k: 'image_cache_webp', label: '新图片自动转 WebP', t: 'bool' },
          { k: 'image_cache_quality', label: 'WebP 质量', t: 'number', hint: '60–95，默认 85；只保存体积更小的转换结果' },
          { k: 'image_cache_max_gb', label: '网页缓存目标容量（GiB）', t: 'number', hint: '默认 5；重要封面与无可确认来源的图片受保护，可能超过目标' },
          { k: 'image_cache_days', label: '普通缓存保留天数', t: 'number', hint: '默认 30，按最后访问时间' },
          { k: 'image_still_days', label: '网页剧照保留天数', t: 'number', hint: '默认 7；不清理媒体目录 extrafanart' },
          { k: 'image_cache_auto_clean', label: '每天自动清理网页缓存', t: 'bool', hint: '默认关闭；建议先在维护页预览，确认后开启' },
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
        title: '下载器索引',
        hint: '定时更新数据库，查询优先复用近期快照；转移和删除成功后即时更新',
        fields: [
          { k: 'download_index_interval', label: '下载器索引更新间隔（分钟）', ph: '30',
            hint: '轻量同步种子状态；已完成任务的文件清单最多每 24 小时重新确认' },
          { k: 'download_index_batch_size', label: '每轮更新文件清单数', ph: '128',
            hint: '每个下载器每轮最多更新这些种子，积压条目自动轮换，减少内存峰值' },
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
      // 翻译按「一家一张卡」拆开。九个字段平铺在一起时看不出哪几个属于
      // 同一家，也看不出实际配了几家；拆开后每张卡是一个独立的选择，
      // 卡头还能标出「使用中 / 未配置」，降级链的现状一眼可见
      {
        title: '翻译降级链',
        panel: 'translate-chain',
        hint: '按 AI → 腾讯 → 百度 → Google 依次尝试，前一家没出结果就换下一家。'
          + '四家都是可选的，配哪家用哪家',
        fields: [],
      },
      {
        title: 'AI 翻译',
        provider: 'ai',
        hint: 'OpenAI 兼容接口。质量最好，但片名露骨时网关常连着 200 回一句'
          + '拒绝说明而不是译文 —— 所以别只配这一家',
        fields: [
          { k: 'openai_url', label: '接口地址', ph: 'https://api.openai.com/v1' },
          { k: 'openai_model', label: '模型', ph: 'gpt-4o-mini' },
          { k: 'openai_api_key', label: 'API Key', t: 'password' },
        ],
      },
      {
        title: '腾讯翻译',
        provider: 'tencent',
        hint: '每月 500 万字符免费。密钥在腾讯云「访问管理 → API 密钥管理」签发，'
          + '需授权 QcloudTMTFullAccess',
        fields: [
          { k: 'tencent_secret_id', label: 'SecretId' },
          { k: 'tencent_secret_key', label: 'SecretKey', t: 'password' },
          { k: 'tencent_region', label: '地域', ph: 'ap-guangzhou' },
        ],
      },
      {
        title: '百度翻译',
        provider: 'baidu',
        hint: '有免费额度，日译中质量一般',
        fields: [
          { k: 'baidu_app_id', label: 'AppID' },
          { k: 'baidu_api_key', label: 'Key', t: 'password' },
        ],
      },
      {
        title: 'Google 翻译',
        provider: 'google',
        hint: '质量最好，但没有免费额度 —— 项目必须在 Cloud Console 绑定结算账号，'
          + '否则每次调用都返回 403',
        fields: [
          { k: 'google_api_key', label: 'API Key', t: 'password' },
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

watch(() => route.query.section, value => {
  if (GROUPS.some(group => group.key === value)) activeGroup.value = value
}, { immediate: true })

async function load() {
  loading.value = true
  try {
    const data = await getConfig()
    Object.assign(form, data || {})
    form.scrape_duplicate_policy = data?.scrape_duplicate_policy || 'versions'
    form.scrape_watermark = {
      enabled: false, targets: 'poster,thumb', types: 'sub,crack,leak,uncensored,censored,resolution',
      layout: 'stack', corner: 'top_left', scale: 0.07, offset_x: 0.015, offset_y: 0.015, gap: 0.01,
      ...Object.fromEntries(['sub', 'crack', 'leak', 'uncensored', 'censored', 'resolution'].map(k => [`position_${k}`, 'auto'])),
      ...(data?.scrape_watermark || {}),
    }
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
  if (ptSiteDisabled(target)) return
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
  <div class="flex h-full min-h-0 min-w-0 flex-col gap-4 overflow-hidden lg:flex-row">
    <!-- 分组导航。窄屏横向滚动一排，宽屏收成左侧竖栏。
         宽屏时右侧内容很长，导航 sticky 钉在顶上不跟着滚；自身超过一屏才内部滚动 -->
    <nav
      class="flex max-h-[35%] shrink-0 gap-4 overflow-auto overscroll-contain px-1 pb-2 lg:max-h-full
             lg:w-44 lg:flex-col lg:pb-0"
    >
      <div v-for="cat in CATEGORIES" :key="cat.key" class="shrink-0 lg:shrink">
        <p class="mb-1.5 hidden px-1 text-[11px] font-medium uppercase tracking-wide text-gray-600 lg:block">
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

    <div class="min-h-0 min-w-0 flex-1 space-y-4 overflow-auto overscroll-contain" tabindex="0" aria-label="设置内容">
    <LoadingBlock v-if="loading" :rows="6" />

    <template v-else>
      <div v-for="group in GROUPS" v-show="activeGroup === group.key" :key="group.key" class="space-y-4">
        <MihomoPanel v-if="group.key === 'network' && activeGroup === 'network'" />
        <div v-if="group.key === 'scrape'" class="card space-y-3">
          <div class="flex flex-wrap items-center justify-between gap-2"><div><p class="font-medium">刮削设置</p><p class="mt-1 text-xs text-gray-500">长期规则在这里设置，视频操作与任务进度在刮削工作台。</p></div><RouterLink :to="{ name: 'Scrape' }" class="text-sm text-brand">打开工作台 ↗</RouterLink></div>
          <div class="flex flex-wrap gap-2" role="tablist" aria-label="刮削设置分类"><button v-for="[value, label] in SCRAPE_TABS" :key="value" class="btn text-xs" :class="scrapeTab === value ? 'bg-brand text-white' : 'btn-ghost'" role="tab" :aria-selected="scrapeTab === value" @click="scrapeTab = value">{{ label }}</button></div>
        </div>
        <component :is="section.collapsible ? 'details' : 'div'" v-for="section in group.sections" v-show="group.key !== 'scrape' || section.tab === scrapeTab" :key="section.title" class="card space-y-3">
          <component :is="section.collapsible ? 'summary' : 'div'" :class="section.collapsible ? 'cursor-pointer' : ''">
            <div class="flex items-center gap-2">
              <p class="text-sm font-medium text-gray-300">{{ section.title }}</p>
              <!-- 翻译各家标出是否生效，省得对着一堆空框猜配没配 -->
              <span
                v-if="section.provider"
                class="rounded px-1.5 py-0.5 text-[10px]"
                :class="translateStatus[section.provider]
                  ? 'bg-emerald-950/60 text-emerald-400'
                  : 'bg-gray-800 text-gray-500'"
              >
                {{ translateStatus[section.provider] ? '使用中' : '未配置' }}
              </span>
            </div>
            <p v-if="section.hint" class="mt-0.5 text-[11px] text-gray-600">
              {{ section.hint }}
            </p>
          </component>

          <div class="grid gap-4 sm:grid-cols-2">
            <ConfigField
              v-for="field in section.fields"
              :key="field.k"
              :field="field"
              :form="form"
              :options="field.k === 'primary_site' ? ptSiteOptions : null"
            />
          </div>

          <!-- 翻译降级链概览：把四张卡的配置状态汇总成一句话 -->
          <WatermarkPreview v-if="section.panel === 'watermark' && activeGroup === 'scrape' && scrapeTab === 'images'" :config="form.scrape_watermark" />
          <template v-if="section.panel === 'translate-chain'">
            <div
              v-if="translateChain.length"
              class="flex flex-wrap items-center gap-1.5 rounded border border-gray-800 bg-gray-900/40 p-2.5"
            >
              <span class="text-[11px] text-gray-500">实际顺序：</span>
              <template v-for="(p, i) in translateChain" :key="p.key">
                <span class="rounded bg-emerald-950/60 px-1.5 py-0.5 text-[11px] text-emerald-400">
                  {{ p.label }}
                </span>
                <span v-if="i < translateChain.length - 1" class="text-[11px] text-gray-600">→</span>
              </template>
            </div>
            <div v-else class="rounded border border-amber-900/60 bg-amber-950/30 p-2.5">
              <p class="text-xs text-amber-400">还没配任何翻译接口</p>
              <p class="mt-0.5 text-[11px] text-gray-500">
                标题会一直显示日文原文，「翻译标题」那个定时任务也不会做事
              </p>
            </div>
            <div
              v-if="translateChain.length === 1 && translateChain[0].key === 'ai'"
              class="rounded border border-amber-900/60 bg-amber-950/30 p-2.5"
            >
              <p class="text-xs text-amber-400">只配了 AI 一家</p>
              <p class="mt-0.5 text-[11px] text-gray-500">
                片名露骨时 AI 网关经常拒绝翻译，那批标题就永远没有译文了。
                建议再配一家传统翻译接口兜底 —— 它们不对内容作道德判断
              </p>
            </div>
          </template>

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
        </component>

        <!-- 连接测试 -->
        <div v-if="group.tests" class="card space-y-2">
          <p class="text-sm font-medium text-gray-300">连接测试</p>
          <div class="flex flex-wrap gap-2">
            <button
              v-for="target in group.tests"
              :key="target"
              class="btn-ghost px-3 py-1.5 text-xs"
              :disabled="testing === target || ptSiteDisabled(target)"
              @click="test(target)"
            >
              {{ ptSiteDisabled(target) ? `${target} · 已停用` : testing === target ? '测试中…' : target }}
            </button>
          </div>
          <div v-for="(result, target) in testResults" :key="target" class="text-xs">
            <span :class="ptSiteDisabled(target) ? 'text-gray-500' : result.success ? 'text-emerald-400' : 'text-red-400'">
              {{ target }}: {{ ptSiteDisabled(target) ? '已停用，凭据保留' : result.message }}
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
