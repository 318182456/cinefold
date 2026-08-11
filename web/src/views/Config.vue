<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import {
  deletePasskey, deleteTelegramWebhook, getConfig, getOidcRedirectUri,
  getTelegramReceive, listPasskeys, listPtSites, passkeyRegisterBegin,
  passkeyRegisterFinish, saveConfig, setTelegramWebhook, testConnection, testOidc,
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

// 字段类型 t: text(默认) / password / bool / select / textarea / tags / size
// 每个分组按用途拆成若干 section，每个 section 一张卡片
const GROUPS = [
  {
    key: 'downloader',
    title: '下载器',
    sections: [
      {
        title: 'qBittorrent',
        fields: [
          { k: 'qbittorrent_url', label: '地址', ph: 'http://192.168.1.10:8080' },
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
    ],
    tests: ['qbittorrent', 'transmission'],
  },
  {
    key: 'media',
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
    ],
    tests: ['emby', 'jellyfin', 'plex'],
  },
  {
    key: 'pt',
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
          { k: 'default_sort', label: '排序规则',
            hint: '逗号分隔，靠前的优先级高，! 前缀表示降权' },
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
    title: '定时任务',
    sections: [
      {
        title: '订阅与下载',
        hint: 'crontab 格式，留空则不执行',
        fields: [
          { k: 'download_schedule_time', label: '订阅下载' },
          { k: 'actor_schedule_time', label: '演员订阅' },
          { k: 'rank_schedule_time', label: '榜单订阅' },
        ],
      },
      {
        title: '数据同步',
        fields: [
          { k: 'sync_hot_time', label: '同步热门' },
          { k: 'sync_brands_time', label: '同步厂牌' },
          { k: 'sync_actors_time', label: '同步演员' },
          { k: 'sync_news', label: '同步新片' },
          { k: 'fill_empty_image_time', label: '补全缺图' },
        ],
      },
      {
        title: '订阅范围',
        fields: [
          { k: 'rank_page', label: '榜单订阅页数', hint: '0 表示不自动订阅' },
          { k: 'rank_type', label: '榜单类型', t: 'select',
            options: ['', 'daily', 'weekly', 'monthly'] },
          { k: 'brand_type', label: '厂牌订阅', hint: 'all 或逗号分隔，如 s1,moodyz' },
        ],
      },
    ],
  },
  {
    key: 'auth',
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
          { k: 'webauthn_rp_name', label: '显示名称', ph: 'byte-muse' },
        ],
        panel: 'passkey',
      },
    ],
  },
  {
    key: 'other',
    title: '其他',
    sections: [
      {
        title: '网络',
        fields: [
          { k: 'proxy', label: 'HTTP/SOCKS 代理', ph: 'socks5://127.0.0.1:7890' },
          { k: 'bypass_url', label: '反爬绕过服务', ph: 'http://127.0.0.1:8191/v1',
            hint: 'FlareSolverr 等，直连被拦时改走它' },
          { k: 'image_proxy_hosts', label: '图片代理域名',
            hint: '逗号分隔，追加到内置白名单' },
          { k: 'javdb_host', label: 'JavDB 地址' },
        ],
      },
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
        title: 'CloudDrive2',
        fields: [
          { k: 'cloudnas_url', label: '地址' },
          { k: 'cloudnas_username', label: '用户名' },
          { k: 'cloudnas_password', label: '密码', t: 'password' },
          { k: 'cloudnas_savepath', label: '保存路径' },
        ],
      },
      {
        title: '版本检测',
        fields: [
          { k: 'github_token', label: 'GitHub Token', t: 'password',
            hint: '镜像公开时可留空；私有镜像需 read:packages 权限' },
        ],
      },
    ],
  },
]

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

onMounted(async () => {
  await load()
  loadPtSites()
  loadTgReceive()
  loadAuthExtras()
})
</script>

<template>
  <div class="space-y-4">
    <!-- 分组切换 -->
    <div class="flex flex-wrap gap-2">
      <button
        v-for="group in GROUPS"
        :key="group.key"
        class="btn px-3 py-1.5 text-xs"
        :class="activeGroup === group.key ? 'bg-brand text-white' : 'btn-ghost'"
        @click="activeGroup = group.key"
      >
        {{ group.title }}
      </button>
    </div>

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
</template>
