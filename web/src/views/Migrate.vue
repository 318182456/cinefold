<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import {
  listMigrateDatabases,
  getMigrateProgress,
  testMigrateTarget,
  startMigrate,
  getImageCacheStats,
  bulkCancelSubscribe,
  getDashboard,
} from '@/api'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'

const toast = useToast()

const loading = ref(true)
const files = ref([])
const current = ref({ url: '', is_sqlite: true })
const imageCache = ref(null)

const form = reactive({
  source: '',
  target_url: '',
})

const testing = ref(false)
const testResult = ref(null)
const starting = ref(false)
const progress = ref(null)

// 清理订阅
const stats = ref(null)
const cleanup = reactive({
  mode: 'keep_recent',   // keep_recent / before_date / only_vr
  keepDays: 90,
  beforeDate: '',
})
const cleanupBusy = ref('')
const cleanupPreview = ref(null)

let timer = null

const TABLE_LABELS = {
  code: '番号',
  actor: '演员',
  history: '下载历史',
  user: '账号',
}

const CLEANUP_MODES = [
  { value: 'keep_recent', label: '按天数' },
  { value: 'before_date', label: '按日期' },
  { value: 'only_vr', label: '只清 VR' },
]

const running = computed(() => progress.value?.running === true)

// 迁移带入了老库账号时要提醒用户：登录用的可能不再是当前用户名
const migratedUser = computed(() => {
  const p = progress.value
  if (!p || p.running || p.dry_run) return false
  return (p.tables || []).some((t) => t.table === 'user' && t.migrated > 0)
})

const selected = computed(() =>
  files.value.find((f) => f.name === form.source) || null,
)

function formatSize(bytes) {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`
}

function tableSummary(file) {
  const entries = Object.entries(file.tables || {})
  if (!entries.length) return '未识别到可迁移的表'
  return entries
    .map(([name, count]) => `${TABLE_LABELS[name] || name} ${count}`)
    .join(' · ')
}

async function load() {
  loading.value = true
  try {
    const data = await listMigrateDatabases()
    files.value = data.files || []
    current.value = data.current || { url: '', is_sqlite: true }

    // 默认选中体积最大的那个库，通常就是要迁的老库
    if (!form.source && files.value.length) {
      const biggest = [...files.value].sort((a, b) => b.size - a.size)[0]
      form.source = biggest.name
    }
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

async function loadImageCache() {
  try {
    imageCache.value = await getImageCacheStats()
  } catch {
    imageCache.value = null
  }
}

async function loadStats() {
  try {
    stats.value = await getDashboard()
  } catch {
    stats.value = null
  }
}

/** 把界面上的选择转成接口参数 */
function cleanupPayload(dryRun) {
  const payload = { dry_run: dryRun }
  if (cleanup.mode === 'keep_recent') {
    payload.keep_recent_days = Number(cleanup.keepDays) || 0
  } else if (cleanup.mode === 'before_date') {
    payload.before_date = cleanup.beforeDate
  } else {
    payload.only_vr = true
  }
  return payload
}

async function previewCleanup() {
  if (cleanup.mode === 'before_date' && !cleanup.beforeDate) {
    return toast.error('请先选择日期')
  }
  cleanupBusy.value = 'preview'
  try {
    cleanupPreview.value = await bulkCancelSubscribe(cleanupPayload(true))
  } catch (err) {
    cleanupPreview.value = null
    toast.error(err.message)
  } finally {
    cleanupBusy.value = ''
  }
}

async function applyCleanup() {
  const matched = cleanupPreview.value?.matched || 0
  if (!matched) return
  // 取消订阅不可逆，执行前再确认一次
  if (!window.confirm(`确定取消这 ${matched} 个订阅？该操作不可撤销。`)) return

  cleanupBusy.value = 'apply'
  try {
    const data = await bulkCancelSubscribe(cleanupPayload(false))
    toast.success(`已取消 ${data.cancelled} 个订阅`)
    cleanupPreview.value = null
    await loadStats()
  } catch (err) {
    toast.error(err.message)
  } finally {
    cleanupBusy.value = ''
  }
}

async function pollProgress() {
  try {
    const data = await getMigrateProgress()
    progress.value = data
    if (data && !data.running) stopPolling()
  } catch {
    stopPolling()
  }
}

function startPolling() {
  if (timer) return
  timer = setInterval(pollProgress, 1500)
}

function stopPolling() {
  clearInterval(timer)
  timer = null
}

async function test() {
  if (!form.source) return toast.error('请先选择源数据库')
  testing.value = true
  try {
    const data = await testMigrateTarget({ ...form })
    testResult.value = data
    if (data.success) toast.success('目标库连接正常')
    else toast.error(data.message)
  } catch (err) {
    testResult.value = { success: false, message: err.message }
    toast.error(err.message)
  } finally {
    testing.value = false
  }
}

async function run(dryRun) {
  if (!form.source) return toast.error('请先选择源数据库')
  if (!dryRun) {
    const label = selected.value ? selected.value.name : form.source
    if (!window.confirm(
      `确定把 ${label} 迁移到 PostgreSQL？\n\n` +
      '已存在的主键会被跳过，不会覆盖目标库现有数据。',
    )) return
  }

  starting.value = true
  try {
    await startMigrate({ ...form, dry_run: dryRun })
    toast.success(dryRun ? '试算已启动' : '迁移已启动')
    progress.value = null
    await pollProgress()
    startPolling()
  } catch (err) {
    toast.error(err.message)
  } finally {
    starting.value = false
  }
}

onMounted(async () => {
  await Promise.all([load(), loadImageCache(), loadStats()])
  await pollProgress()
  if (running.value) startPolling()
})

onUnmounted(stopPolling)
</script>

<template>
  <div class="space-y-4">
    <!-- 当前数据库 -->
    <div class="card space-y-2">
      <p class="text-sm font-medium text-gray-300">当前数据库</p>
      <p class="break-all font-mono text-xs text-gray-400">{{ current.url || '—' }}</p>
      <p v-if="current.is_sqlite" class="text-[11px] text-amber-400">
        当前运行在 SQLite 上。迁移到 PostgreSQL 后，需在设置里填好 DATABASE_URL 并重启才会真正切换。
      </p>
      <p v-else class="text-[11px] text-emerald-400">已运行在外部数据库上</p>
    </div>

    <!-- 图片缓存概览 -->
    <div v-if="imageCache" class="card space-y-1">
      <p class="text-sm font-medium text-gray-300">图片缓存</p>
      <p v-if="imageCache.exists" class="text-xs text-gray-400">
        已缓存 <span class="text-emerald-400">{{ imageCache.codes }}</span> 个番号目录，
        抽查 {{ imageCache.probed }} 个中
        <span class="text-emerald-400">{{ imageCache.with_banner }}</span> 个有封面
        <span v-if="imageCache.sampled" class="text-gray-600">（抽样）</span>
      </p>
      <p v-else class="text-xs text-gray-500">缓存目录尚未创建</p>
      <p v-if="imageCache.dir" class="font-mono text-[11px] text-gray-600">{{ imageCache.dir }}</p>
    </div>

    <!-- 清理订阅 -->
    <div class="card space-y-3">
      <div>
        <p class="text-sm font-medium text-gray-300">清理订阅</p>
        <p class="mt-0.5 text-[11px] text-gray-600">
          只取消「已订阅」的番号，下载中／已下载／已入库的不受影响
        </p>
      </div>

      <p v-if="stats" class="text-xs text-gray-400">
        当前已订阅
        <span class="text-brand">{{ stats.subscribed }}</span> 个，
        番号总数 {{ stats.total }}
      </p>

      <div class="flex flex-wrap gap-2">
        <button
          v-for="opt in CLEANUP_MODES"
          :key="opt.value"
          class="btn px-3 py-1.5 text-xs"
          :class="cleanup.mode === opt.value ? 'bg-brand text-white' : 'btn-ghost'"
          @click="cleanup.mode = opt.value; cleanupPreview = null"
        >
          {{ opt.label }}
        </button>
      </div>

      <div v-if="cleanup.mode === 'keep_recent'" class="flex items-center gap-2">
        <span class="text-xs text-gray-400">保留最近</span>
        <input
          v-model="cleanup.keepDays"
          type="number"
          min="1"
          class="input w-24"
          @input="cleanupPreview = null"
        />
        <span class="text-xs text-gray-400">天发行的，更早的取消</span>
      </div>

      <div v-else-if="cleanup.mode === 'before_date'" class="flex items-center gap-2">
        <span class="text-xs text-gray-400">取消</span>
        <input
          v-model="cleanup.beforeDate"
          type="date"
          class="input w-44"
          @input="cleanupPreview = null"
        />
        <span class="text-xs text-gray-400">之前发行的</span>
      </div>

      <p v-else class="text-xs text-gray-400">
        取消所有 VR 作品的订阅，按番号与标题识别
      </p>

      <div class="flex flex-wrap gap-2">
        <button
          class="btn-ghost px-3 py-1.5 text-xs"
          :disabled="cleanupBusy === 'preview'"
          @click="previewCleanup"
        >
          {{ cleanupBusy === 'preview' ? '统计中…' : '试算' }}
        </button>
        <button
          v-if="cleanupPreview?.matched"
          class="btn px-3 py-1.5 text-xs bg-red-900/70 text-red-100 hover:bg-red-900"
          :disabled="cleanupBusy === 'apply'"
          @click="applyCleanup"
        >
          {{ cleanupBusy === 'apply' ? '执行中…' : `取消这 ${cleanupPreview.matched} 个订阅` }}
        </button>
      </div>

      <div v-if="cleanupPreview" class="space-y-2 border-t border-gray-800 pt-3">
        <p v-if="!cleanupPreview.matched" class="text-xs text-gray-500">
          没有符合条件的订阅
        </p>
        <template v-else>
          <p class="text-xs text-amber-400">
            将取消 {{ cleanupPreview.matched }} 个订阅，以下是前
            {{ cleanupPreview.samples.length }} 条：
          </p>
          <div class="max-h-56 space-y-1 overflow-y-auto">
            <div
              v-for="item in cleanupPreview.samples"
              :key="item.code"
              class="flex gap-2 text-[11px]"
            >
              <span class="w-28 shrink-0 font-mono text-gray-300">{{ item.code }}</span>
              <span class="w-20 shrink-0 text-gray-600">{{ item.release_date || '—' }}</span>
              <span class="truncate text-gray-500">{{ item.title }}</span>
            </div>
          </div>
        </template>
      </div>
    </div>

    <LoadingBlock v-if="loading" :rows="4" />

    <template v-else>
      <!-- 选择源库 -->
      <div class="card space-y-3">
        <p class="text-sm font-medium text-gray-300">选择要迁移的 SQLite 库</p>

        <EmptyState
          v-if="!files.length"
          text="数据目录下没有找到 SQLite 文件"
          hint="确认老库已挂载到 /data 下"
        />

        <label
          v-for="file in files"
          :key="file.name"
          class="flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors"
          :class="form.source === file.name
            ? 'border-brand bg-brand/5'
            : 'border-gray-800 hover:border-gray-700'"
        >
          <input
            v-model="form.source"
            type="radio"
            :value="file.name"
            :disabled="running"
            class="mt-1 h-4 w-4 accent-emerald-500"
          />
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-2">
              <span class="font-mono text-sm text-gray-200">{{ file.name }}</span>
              <span class="text-[11px] text-gray-500">{{ formatSize(file.size) }}</span>
              <span class="text-[11px] text-gray-600">{{ file.modified }}</span>
            </div>
            <p class="mt-1 text-xs text-gray-500">{{ tableSummary(file) }}</p>
          </div>
        </label>
      </div>

      <!-- 目标库 -->
      <div class="card space-y-3">
        <div>
          <label class="label">目标 PostgreSQL 连接串</label>
          <input
            v-model="form.target_url"
            class="input font-mono text-xs"
            :disabled="running"
            placeholder="postgresql://cinefold:cinefold@postgres:5432/cinefold"
          />
          <p class="mt-1 text-[11px] text-gray-600">
            留空则使用当前配置的 DATABASE_URL；目标库不存在的表会自动创建
          </p>
        </div>

        <div class="flex flex-wrap gap-2">
          <button
            class="btn-ghost px-3 py-1.5 text-xs"
            :disabled="testing || running"
            @click="test"
          >
            {{ testing ? '测试中…' : '测试连接' }}
          </button>
          <button
            class="btn-ghost px-3 py-1.5 text-xs"
            :disabled="starting || running"
            @click="run(true)"
          >
            试算（不写入）
          </button>
          <button
            class="btn-primary px-3 py-1.5 text-xs"
            :disabled="starting || running || !files.length"
            @click="run(false)"
          >
            {{ running ? '迁移中…' : '开始迁移' }}
          </button>
        </div>

        <p v-if="testResult" class="text-xs">
          <span :class="testResult.success ? 'text-emerald-400' : 'text-red-400'">
            {{ testResult.message }}
          </span>
        </p>
      </div>

      <!-- 进度 -->
      <div v-if="progress" class="card space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <p class="text-sm font-medium text-gray-300">
            {{ progress.dry_run ? '试算结果' : '迁移进度' }}
          </p>
          <span
            class="badge"
            :class="progress.running
              ? 'bg-amber-900 text-amber-300'
              : progress.error
                ? 'bg-red-900 text-red-300'
                : 'bg-emerald-900 text-emerald-300'"
          >
            {{ progress.running ? '执行中' : progress.error ? '失败' : '已完成' }}
          </span>
        </div>

        <div class="space-y-1 text-xs text-gray-500">
          <p>源库 <span class="font-mono text-gray-400">{{ progress.source }}</span></p>
          <p class="break-all">目标 <span class="font-mono text-gray-400">{{ progress.target }}</span></p>
          <p v-if="progress.started_at">开始 {{ progress.started_at }}</p>
          <p v-if="progress.finished_at">结束 {{ progress.finished_at }}</p>
          <p v-if="progress.running && progress.current_table" class="text-amber-400">
            正在处理 {{ TABLE_LABELS[progress.current_table] || progress.current_table }}
          </p>
        </div>

        <p v-if="progress.error" class="break-all text-xs text-red-400">
          {{ progress.error }}
        </p>

        <div v-if="progress.tables?.length" class="overflow-x-auto">
          <table class="w-full text-xs">
            <thead class="text-gray-500">
              <tr>
                <th class="py-1 text-left font-medium">表</th>
                <th class="py-1 text-right font-medium">源行数</th>
                <th class="py-1 text-right font-medium">已写入</th>
                <th class="py-1 text-right font-medium">跳过</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in progress.tables" :key="row.table" class="border-t border-gray-800">
                <td class="py-1.5 text-gray-300">
                  {{ TABLE_LABELS[row.table] || row.table }}
                  <span v-if="row.error" class="ml-1 text-red-400">{{ row.error }}</span>
                </td>
                <td class="py-1.5 text-right tabular-nums text-gray-400">{{ row.source_rows }}</td>
                <td class="py-1.5 text-right tabular-nums text-emerald-400">{{ row.migrated }}</td>
                <td class="py-1.5 text-right tabular-nums"
                    :class="row.skipped ? 'text-amber-400' : 'text-gray-600'">
                  {{ row.skipped }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <p v-if="!progress.running && !progress.dry_run" class="text-xs text-gray-500">
          共写入 <span class="text-emerald-400">{{ progress.total_migrated }}</span> 行。
          主键已存在的行会被跳过，可重复执行。
        </p>

        <p v-if="migratedUser" class="text-xs text-amber-400">
          老库的账号也一并迁入了。若老库用户名与当前账号不同，请用老库的用户名密码登录。
        </p>
      </div>
    </template>
  </div>
</template>
