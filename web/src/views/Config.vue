<script setup>
import { onMounted, reactive, ref } from 'vue'
import { getConfig, saveConfig, testConnection } from '@/api'
import { useToast } from '@/composables/useToast'
import { useConfigStore } from '@/stores/config'
import LoadingBlock from '@/components/LoadingBlock.vue'
import TagInput from '@/components/TagInput.vue'

const toast = useToast()
const configStore = useConfigStore()

const form = reactive({})
const loading = ref(true)
const saving = ref(false)
const activeGroup = ref('downloader')
const testing = ref('')
const testResults = reactive({})

// t: text(默认) / password / bool / select / textarea / tags
const GROUPS = [
  {
    key: 'downloader',
    title: '下载器',
    fields: [
      { k: 'qbittorrent_url', label: 'qBittorrent 地址', ph: 'http://192.168.1.10:8080' },
      { k: 'qbittorrent_username', label: '用户名' },
      { k: 'qbittorrent_password', label: '密码', t: 'password' },
      { k: 'qbittorrent_download_path', label: '下载路径' },
      { k: 'qbittorrent_category', label: '分类' },
      { k: 'qbittorrent_verify_cert', label: '校验 HTTPS 证书', t: 'bool',
        hint: '使用自签证书或反代时保持关闭' },
      { k: 'transmission_url', label: 'Transmission 地址' },
      { k: 'transmission_username', label: '用户名' },
      { k: 'transmission_password', label: '密码', t: 'password' },
      { k: 'transmission_download_path', label: '下载路径' },
      { k: 'transmission_label', label: '标签' },
    ],
    tests: ['qbittorrent', 'transmission'],
  },
  {
    key: 'media',
    title: '媒体库',
    fields: [
      { k: 'emby_url', label: 'Emby 地址' },
      { k: 'emby_api_key', label: 'Emby API Key', t: 'password' },
      { k: 'jellyfin_url', label: 'Jellyfin 地址' },
      { k: 'jellyfin_api_key', label: 'Jellyfin API Key', t: 'password' },
      { k: 'jellyfin_user', label: 'Jellyfin 用户名' },
      { k: 'plex_url', label: 'Plex 地址' },
      { k: 'plex_token', label: 'Plex Token', t: 'password' },
      { k: 'enable_auto_complete', label: '跳过已入库资源', t: 'bool' },
    ],
    tests: ['emby', 'jellyfin', 'plex'],
  },
  {
    key: 'pt',
    title: 'PT 站点',
    fields: [
      { k: 'mteam_api_key', label: 'M-Team API Key', t: 'password' },
      { k: 'rousi_username', label: 'Rousi 用户名',
        hint: '填了账号密码会自动登录续期' },
      { k: 'rousi_password', label: 'Rousi 密码', t: 'password' },
      { k: 'rousi_token', label: 'Rousi Token', t: 'password',
        hint: '填了账号密码可留空；手动填时到期需自行更新' },
      { k: 'rousi_passkey', label: 'Rousi Passkey', t: 'password',
        hint: '取自 announce 地址中间那段' },
      { k: 'ptt_cookie', label: 'PTT Cookie', t: 'password' },
      { k: 'nicept_cookie', label: 'NicePT Cookie', t: 'password' },
    ],
    tests: ['mteam'],
  },
  {
    key: 'bt',
    title: '自定义 BT 源',
    fields: [
      { k: 'bt_url', label: '请求地址', hint: '支持 ${keyword} 占位符' },
      { k: 'bt_method', label: '请求方法', t: 'select', options: ['get', 'post'] },
      { k: 'bt_header', label: '请求头 JSON', t: 'textarea' },
      { k: 'bt_json_data', label: '请求体 JSON', t: 'textarea' },
    ],
  },
  {
    key: 'notify',
    title: '通知',
    fields: [
      { k: 'telegram_bot_token', label: 'Telegram Bot Token', t: 'password' },
      { k: 'telegram_chat_id', label: 'Telegram Chat ID' },
      { k: 'telegram_spoiler', label: '图片防剧透', t: 'bool' },
      { k: 'telegram_whitelist', label: '白名单', hint: '多个用 | 分隔' },
      { k: 'msg_allow_prefixes', label: '番号前缀白名单', t: 'tags', upper: true,
        ph: '留空则不限制',
        hint: '消息订阅只接受这些前缀；留空表示全部接受',
        suggestions: ['SSIS', 'NHDTB', 'JUL', 'ABP', 'IPX', 'MIDE', 'FC2'] },
      { k: 'msg_block_prefixes', label: '番号前缀黑名单', t: 'tags', upper: true,
        ph: '留空则不屏蔽',
        hint: '优先于白名单，命中即不订阅',
        suggestions: ['NHDTA', 'NHDTB', 'FC2'] },
      { k: 'msg_max_codes', label: '单条消息番号上限',
        hint: '超出部分不订阅并在回复中列出，0 表示不限' },
      { k: 'msg_auto_download', label: '订阅后立即检索', t: 'bool',
        hint: '开启后消息订阅会在后台搜种并推给下载器；关闭则等每日订阅下载任务' },
      { k: 'wechat_corp_id', label: '企业微信 CorpID' },
      { k: 'wechat_corp_secret', label: '企业微信 Secret', t: 'password' },
      { k: 'wechat_agent_id', label: '企业微信 AgentID' },
      { k: 'wechat_to_user', label: '推送用户' },
      { k: 'wechat_banner', label: '推送横幅图', t: 'bool' },
    ],
    tests: ['telegram', 'wechat'],
  },
  {
    key: 'filter',
    title: '过滤与排序',
    fields: [
      { k: 'default_sort', label: '排序规则', hint: '逗号分隔，! 前缀表示降权' },
      { k: 'max_actor', label: '最大共演人数' },
      { k: 'main_site', label: '主资源站', t: 'select', options: ['ALL', 'javdb', 'javbus'] },
      { k: 'image_mode', label: '图片模式', t: 'select', options: ['BLUR', 'VISIBLE', 'INVISIBLE'] },
      { k: 'enable_photo_cache', label: '图片持久化', t: 'bool' },
    ],
  },
  {
    key: 'schedule',
    title: '定时任务',
    fields: [
      { k: 'download_schedule_time', label: '订阅下载', hint: 'crontab 格式' },
      { k: 'actor_schedule_time', label: '演员订阅' },
      { k: 'rank_schedule_time', label: '榜单订阅' },
      { k: 'sync_hot_time', label: '同步热门' },
      { k: 'sync_brands_time', label: '同步厂牌' },
      { k: 'sync_actors_time', label: '同步演员' },
      { k: 'sync_news', label: '同步新片' },
      { k: 'fill_empty_image_time', label: '补全缺图' },
      { k: 'rank_page', label: '榜单订阅页数', hint: '0 表示不自动订阅' },
      { k: 'rank_type', label: '榜单类型', t: 'select', options: ['', 'daily', 'weekly', 'monthly'] },
      { k: 'brand_type', label: '厂牌订阅', hint: 'all 或逗号分隔，如 s1,moodyz' },
    ],
  },
  {
    key: 'other',
    title: '其他',
    fields: [
      { k: 'proxy', label: 'HTTP/SOCKS 代理', ph: 'socks5://127.0.0.1:7890' },
      { k: 'bypass_url', label: '反爬绕过服务', ph: 'http://127.0.0.1:8191/v1', hint: 'FlareSolverr 等，直连被拦时改走它' },
      { k: 'image_proxy_hosts', label: '图片代理域名', hint: '逗号分隔，追加到内置白名单' },
      { k: 'github_token', label: 'GitHub Token', t: 'password',
        hint: '检测新版本用。镜像公开时可留空；私有镜像需 read:packages 权限' },
      { k: 'javdb_host', label: 'JavDB 地址' },
      { k: 'external_domain', label: '外网访问地址' },
      { k: 'openai_url', label: 'AI 翻译接口' },
      { k: 'openai_model', label: 'AI 模型' },
      { k: 'openai_api_key', label: 'AI API Key', t: 'password' },
      { k: 'baidu_app_id', label: '百度翻译 AppID' },
      { k: 'baidu_api_key', label: '百度翻译 Key', t: 'password' },
      { k: 'google_api_key', label: 'Google 翻译 Key', t: 'password' },
      { k: 'cloudnas_url', label: 'CloudDrive2 地址' },
      { k: 'cloudnas_username', label: 'CD2 用户名' },
      { k: 'cloudnas_password', label: 'CD2 密码', t: 'password' },
      { k: 'cloudnas_savepath', label: 'CD2 保存路径' },
    ],
  },
]

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
    group.fields.forEach((field) => {
      payload[field.k] = form[field.k]
    })
    await saveConfig(payload)
    toast.success('配置已保存')
    await configStore.load(true)
  } catch (err) {
    toast.error(err.message)
  } finally {
    saving.value = false
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

onMounted(load)
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
        <div class="card grid gap-4 sm:grid-cols-2">
          <div v-for="field in group.fields" :key="field.k">
            <label class="label">{{ field.label }}</label>

            <label v-if="field.t === 'bool'" class="flex items-center gap-2">
              <input v-model="form[field.k]" type="checkbox" class="h-4 w-4 accent-emerald-500" />
              <span class="text-sm text-gray-400">启用</span>
            </label>

            <select v-else-if="field.t === 'select'" v-model="form[field.k]" class="input">
              <option v-for="option in field.options" :key="option" :value="option">
                {{ option || '（默认）' }}
              </option>
            </select>

            <textarea
              v-else-if="field.t === 'textarea'"
              v-model="form[field.k]"
              class="input font-mono text-xs"
              rows="3"
            />

            <TagInput
              v-else-if="field.t === 'tags'"
              v-model="form[field.k]"
              :placeholder="field.ph || '输入后回车添加'"
              :suggestions="field.suggestions || []"
              :uppercase="!!field.upper"
            />

            <input
              v-else
              v-model="form[field.k]"
              :type="field.t === 'password' ? 'password' : 'text'"
              class="input"
              :placeholder="field.ph || ''"
            />

            <p v-if="field.hint" class="mt-1 text-[11px] text-gray-600">{{ field.hint }}</p>
          </div>
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
