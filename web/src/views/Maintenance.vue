<script setup>
import { useVisibleTimers } from '@/composables/useVisibleTimers'
const { setTimeout, clearTimeout, setInterval, clearInterval } = useVisibleTimers()
import PagePanel from '@/components/PagePanel.vue'
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import http, { getImageCacheStats, bulkCancelSubscribe, getDashboard, redetectPortrait, getRedetectProgress } from '@/api'
import { useToast } from '@/composables/useToast'

const toast = useToast()
const imageCache = ref(null)
const storage = ref({ phase: 'idle', running: false })
const storageBusy = ref(false)
const memory = ref(null)
async function refreshMemory() {
  try { memory.value = await http.get('/runtime/memory') } catch (err) { toast.error(err.message) }
}
let storageTimer
const bytes = value => value == null ? '待统计' : `${(value / 1024 ** 3).toFixed(2)} GiB`
async function pollStorage() {
  clearTimeout(storageTimer)
  try {
    storage.value = await http.get('/image-cache/maintenance')
    if (storage.value.running) storageTimer = setTimeout(pollStorage, 2500)
  } catch (err) { toast.error(err.message) }
}
async function maintainImages(mode, resume = false) {
  if (mode === 'clean' && !resume && !window.confirm('清理可重新获取的网页缓存？重要封面保留，不操作媒体目录。')) return
  storageBusy.value = true
  try {
    storage.value = await http.post('/image-cache/maintenance', { mode, resume })
    await pollStorage()
  } catch (err) { toast.error(err.message) }
  finally { storageBusy.value = false }
}
async function stopStorage() {
  await http.post('/image-cache/maintenance/stop')
  await pollStorage()
}
// 清理订阅
const stats = ref(null)
const cleanup = reactive({
  mode: 'keep_recent',   // keep_recent / before_date / only_vr
  keepDays: 90,
  beforeDate: '',
})
const cleanupBusy = ref('')
const cleanupPreview = ref(null)

// 封面人像面重判
const redetecting = ref(false)
const redetectProgress = ref(null)

let portraitTimer = null

const CLEANUP_MODES = [
  { value: 'keep_recent', label: '按天数' },
  { value: 'before_date', label: '按日期' },
  { value: 'only_vr', label: '只清 VR' },
]

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

async function pollPortrait() {
  try {
    const data = await getRedetectProgress()
    redetectProgress.value = data
    if (data && !data.running) {
      stopPortraitPolling()
      redetecting.value = false
    }
  } catch {
    stopPortraitPolling()
    redetecting.value = false
  }
}

function stopPortraitPolling() {
  clearInterval(portraitTimer)
  portraitTimer = null
}

async function redetect() {
  redetecting.value = true
  try {
    await redetectPortrait(false)
    toast.success('已开始重新判断封面人像面')
    await pollPortrait()
    // 几万张图要跑一会儿，轮询到后端报完成为止
    if (redetecting.value && !portraitTimer) portraitTimer = setInterval(pollPortrait, 1500)
  } catch (err) {
    redetecting.value = false
    toast.error(err.message)
  }
}


onMounted(async () => {
  await Promise.all([loadImageCache(), loadStats(), pollPortrait(), pollStorage(), refreshMemory()])
  if (redetectProgress.value?.running) {
    redetecting.value = true
    portraitTimer = setInterval(pollPortrait, 1500)
  }
})
onUnmounted(stopPortraitPolling)
onUnmounted(() => clearTimeout(storageTimer))
</script>

<template>
  <PagePanel content-class="space-y-4">
    <div class="card flex flex-wrap items-center gap-3 text-xs text-gray-400">
      <span>Python 进程 RSS {{ memory?.rss == null ? '当前系统不支持采样' : `${(memory.rss / 1048576).toFixed(1)} MiB` }}</span>
      <span v-if="memory?.anonymous != null">匿名内存 {{ (memory.anonymous / 1048576).toFixed(1) }} MiB · 峰值 {{ (memory.peak_rss / 1048576).toFixed(1) }} MiB</span>
      <button class="btn-ghost" @click="refreshMemory">刷新内存</button>
    </div>
    <div class="card space-y-3">
      <p class="text-sm font-medium">网页图片维护</p>
      <p class="text-xs text-gray-500">先预览占用与可清理数量。普通缓存按最后访问时间淘汰；订阅、入库、收藏封面受保护，可能超过容量目标。不清理媒体目录和未知来源图片。</p>
      <div class="flex flex-wrap gap-2">
        <button class="btn-ghost" :disabled="storage.running || storageBusy" @click="maintainImages('preview')">预览缓存</button>
        <button class="btn-ghost" :disabled="storage.running || storageBusy" @click="maintainImages('optimize')">压缩存量图片</button>
        <button class="btn-ghost" :disabled="storage.running || storageBusy || storage.mode !== 'preview' || storage.phase !== 'finished'" @click="maintainImages('clean')">按策略清理</button>
        <button v-if="storage.running" class="btn-ghost" @click="stopStorage">暂停</button>
        <button v-if="['paused', 'interrupted', 'failed'].includes(storage.phase)" class="btn-ghost" :disabled="storageBusy" @click="maintainImages(storage.mode, true)">继续上次任务</button>
      </div>
      <div v-if="storage.phase !== 'idle'" class="space-y-1 text-xs text-gray-400" role="status">
        <p>{{ storage.message }} · 已扫描 {{ storage.scanned || 0 }} 张 · 已处理 {{ storage.processed || 0 }} 张</p>
        <p v-if="storage.stage === 'scan'">已扫描容量 {{ bytes(storage.scanned_bytes) }} · 完整占用待扫描结束后确认</p>
        <p v-else>当前占用 {{ bytes(storage.total_bytes) }}</p>
        <p v-if="storage.mode !== 'optimize'">
          <template v-if="storage.processed || storage.phase === 'finished'">{{ storage.phase === 'finished' ? '可清理' : '已发现可清理' }} {{ storage.candidate_files || 0 }} 张 / {{ bytes(storage.candidate_bytes) }}</template>
          <template v-else>可清理数量待试算</template>
        </p>
        <p>已节省 {{ bytes(storage.saved_bytes) }}</p>
        <p v-if="storage.current" class="break-all font-mono text-gray-500">{{ storage.current }}</p>
        <p v-if="storage.updated_at">最近更新 {{ new Date(storage.updated_at * 1000).toLocaleString() }}</p>
        <p v-for="error in storage.errors || []" :key="error" class="text-amber-400">{{ error }}</p>
      </div>
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

      <!-- 封面人像面：卡片上封面往哪半边偏，靠这个字段决定 -->
      <div class="border-t border-gray-800 pt-2">
        <div class="flex flex-wrap items-center gap-2">
          <button class="btn-ghost px-2.5 py-1 text-xs" :disabled="redetecting" @click="redetect">
            {{ redetecting ? '判断中…' : '重设封面人像面' }}
          </button>
          <span class="text-[11px] text-gray-500">
            重新判断所有封面的人像在哪半边，卡片按结果偏移显示
          </span>
        </div>
        <p v-if="redetectProgress" class="mt-1 text-xs text-gray-400">
          <template v-if="redetectProgress.running">
            {{ redetectProgress.done }} / {{ redetectProgress.total }}
          </template>
          <template v-else-if="redetectProgress.message">
            {{ redetectProgress.message }}
          </template>
        </p>
      </div>
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
        <RouterLink :to="{ name: 'Subscribe', query: { status: '1' } }" class="text-brand hover:underline">{{ stats.subscribed }}</RouterLink> 个，
        番号总数 <RouterLink :to="{ name: 'Subscribe', query: { status: 'all' } }" class="text-brand hover:underline">{{ stats.total }}</RouterLink>
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

  </PagePanel>
</template>
