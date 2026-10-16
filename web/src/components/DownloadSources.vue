<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import http from '@/api'
import { useToast } from '@/composables/useToast'
import ModalDialog from '@/components/ModalDialog.vue'
import ScrapeMetadataDetail from '@/components/ScrapeMetadataDetail.vue'

const emit = defineEmits(['preview'])
const toast = useToast()
const loading = ref(false), inventory = ref({ running: false, files: 0, done: 0, total: 0, errors: [] })
const data = ref({ roots: [], items: [], total: 0, size: 30, counts: {}, warnings: [] })
const root = ref(''), keyword = ref(''), status = ref(''), page = ref(1), selected = ref([])
const job = ref({ running: false, results: [], total: 0, done: 0, ok: 0 })
const dialog = ref(false), drafts = ref([]), submitting = ref(false)
const options = ref({ overwrite: false, refresh_metadata: false, fetch_meta: true })
const detailOpen = ref(false), detailRow = ref(null)
const stats = computed(() => {
  const c = data.value.counts
  return [
    { label: '全部视频', value: Object.values(c).reduce((a, b) => a + b, 0), status: '', color: 'text-gray-200' },
    { label: '等待刮削', value: c.pending || 0, status: 'pending', color: 'text-sky-400' },
    { label: '已完成', value: c.complete || 0, status: 'complete', color: 'text-emerald-400' },
    { label: '需指定番号', value: c.unrecognized || 0, status: 'unrecognized', color: 'text-amber-400' },
    { label: '刮削失败', value: c.failed || 0, status: 'failed', color: 'text-red-400' },
  ]
})
function showDetail(row) { detailRow.value = row; detailOpen.value = true }
function processDetail(detail) { detailOpen.value = false; open([{ ...detailRow.value, code: detail.code, links: detail.links }], !!detail.links.length) }
function filterStatus(value) { status.value = value; search() }
const LABELS = { pending: '未刮削', complete: '已刮削', incomplete: '产物缺失', failed: '刮削失败', unrecognized: '需指定番号', downloading: '下载中', queued: '排队中', running: '刮削中', interrupted: '上次中断', cancelled: '已停止', empty: '空文件' }
let request = 0, timer, indexTimer, disposed = false, initialIndexRequested = false
const selectable = row => !['downloading', 'queued', 'running', 'empty'].includes(row.status)
const allSelected = computed(() => data.value.items.filter(selectable).length > 0 && data.value.items.filter(selectable).every(r => selected.value.includes(r.path)))
const selectedRows = computed(() => data.value.items.filter(r => selected.value.includes(r.path)))
const formatSize = size => size >= 1073741824 ? `${(size / 1073741824).toFixed(2)} GB` : `${(size / 1048576).toFixed(1)} MB`
function toggleAll() { selected.value = allSelected.value ? [] : data.value.items.filter(selectable).map(r => r.path) }
async function load() {
  const id = ++request; loading.value = true
  try {
    const result = await http.get('/scrape/videos', { params: { root: root.value, keyword: keyword.value, status: status.value, page: page.value }, timeout: 180000 })
    if (disposed || id !== request) return
    data.value = result
    inventory.value = result.inventory
    if (result.inventory.running) scheduleIndexPoll()
    else if (result.needs_refresh && !initialIndexRequested) { initialIndexRequested = true; refreshIndex() }
    selected.value = selected.value.filter(path => result.items.some(r => r.path === path && selectable(r)))
    if (!result.items.length && page.value > 1 && result.total > 0) { page.value = Math.ceil(result.total / result.size); await load() }
  } catch (e) { if (!disposed && id === request) toast.error(e.message) } finally { if (id === request) loading.value = false }
}
function search() { page.value = 1; selected.value = []; load() }
function scheduleIndexPoll() {
  clearTimeout(indexTimer)
  if (!disposed) indexTimer = setTimeout(pollIndex, 1500)
}
async function refreshIndex() {
  initialIndexRequested = true
  try {
    inventory.value = await http.post('/scrape/videos/refresh', null, { params: { root: root.value } })
    scheduleIndexPoll()
  } catch (e) { toast.error(e.message) }
}
async function pollIndex() {
  try {
    const result = await http.get('/scrape/videos/refresh')
    if (disposed) return
    inventory.value = result
    if (result.running) scheduleIndexPoll()
    else await load()
  } catch (e) { if (!disposed) toast.error(e.message) }
}
function open(rows, overwrite = false) {
  drafts.value = rows.map(r => ({ path: r.path, name: r.name, code: r.code || '' }))
  options.value = { overwrite, refresh_metadata: false, fetch_meta: true }
  dialog.value = true
}
function preview(row) {
  dialog.value = false
  emit('preview', { path: row.path, code: row.code.trim() })
}
function toggleFetch() { if (!options.value.fetch_meta) options.value.refresh_metadata = false }
async function poll() {
  clearTimeout(timer)
  try {
    const result = await http.get('/scrape/jobs/current')
    if (disposed) return
    const wasRunning = job.value.running
    job.value = result
    if (result.running) timer = setTimeout(poll, 2000)
    else if (wasRunning) {
      await load()
      if (result.error) toast.error(result.error)
      else if (result.cancel_requested) toast.success('已停止后续刮削')
      else if (result.done > result.ok) toast.error(`${result.done - result.ok} 个番号未完成，可在列表中重试`)
      else toast.success(`刮削完成，${result.ok} 个番号`)
    }
  } catch (e) { if (!disposed) { toast.error(e.message); timer = setTimeout(poll, 5000) } }
}
async function start() {
  if (!drafts.value.length || drafts.value.some(r => !r.code.trim())) { toast.error('请为每个视频填写番号'); return }
  submitting.value = true
  try {
    job.value = await http.post('/scrape/jobs', { items: drafts.value.map(({ path, code }) => ({ path, code: code.trim() })), ...options.value })
    dialog.value = false; selected.value = []; await load(); poll()
  } catch (e) { toast.error(e.message) } finally { submitting.value = false }
}
async function cancel() {
  try { job.value = await http.post('/scrape/jobs/current/cancel') } catch (e) { toast.error(e.message) }
}
function changePage(step) { page.value += step; selected.value = []; load() }
defineExpose({ refresh: load })
onMounted(() => { load(); poll() })
onUnmounted(() => { disposed = true; request++; clearTimeout(timer); clearTimeout(indexTimer) })
</script>

<template>
  <section class="space-y-5">
    <div class="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
      <button v-for="item in stats" :key="item.label" class="rounded-xl border p-4 text-left transition hover:bg-gray-800/60 focus-visible:outline focus-visible:outline-brand" :class="status === item.status ? 'border-brand/60 bg-brand/5' : 'border-gray-800 bg-gray-900/40'" :aria-pressed="status === item.status" @click="filterStatus(item.status)">
        <p class="text-xs text-gray-500">{{ item.label }}</p><p class="mt-2 text-2xl font-semibold tabular-nums" :class="item.color">{{ item.value }}</p>
      </button>
    </div>
    <div v-if="job.job_id" class="space-y-3 rounded-xl border border-gray-700 bg-gray-900/50 p-4">
      <div class="flex flex-wrap items-center gap-3"><p class="text-sm text-gray-300">{{ job.running ? '后台刮削中' : job.cancel_requested ? '后续任务已停止' : '最近一次手动刮削' }} · {{ job.done }} / {{ job.total }} 个番号 · 成功 {{ job.ok }} 个</p><button v-if="job.running" class="text-xs text-amber-400 disabled:opacity-40" :disabled="job.cancel_requested" @click="cancel">{{ job.cancel_requested ? '当前番号完成后停止' : '停止后续任务' }}</button></div>
      <div class="h-1.5 overflow-hidden rounded-full bg-gray-800" role="progressbar" aria-label="刮削任务进度" :aria-valuenow="job.done" :aria-valuemin="0" :aria-valuemax="Math.max(job.total, 1)"><div class="h-full rounded-full bg-brand transition-all" :style="{ width: `${Math.min(100, job.total ? job.done / job.total * 100 : 0)}%` }"></div></div>
      <p v-if="job.current" class="text-xs text-brand">当前处理 {{ job.current }}，可离开页面，任务会继续执行。</p><p v-if="job.error" class="text-xs text-red-400">{{ job.error }}</p>
      <details v-if="job.results.length"><summary class="cursor-pointer text-xs text-gray-400">查看逐项结果</summary><div v-for="(row, i) in job.results" :key="i" class="mt-2 border-t border-gray-800 pt-2 text-xs"><p :class="row.ok ? 'text-emerald-400' : 'text-red-400'">{{ row.code }} · {{ row.ok ? '完成' : '失败' }} · NFO {{ row.nfo_written }} · 图片 {{ row.images_written }}</p><p v-if="row.error" class="mt-1 break-all text-red-400">{{ row.error }}</p><p v-for="(warning, n) in row.warnings" :key="n" class="mt-1 text-amber-400">{{ warning }}</p><p v-for="path in row.links" :key="path" class="mt-1 break-all text-gray-500">{{ path }}</p></div></details>
    </div>
    <div class="card space-y-4">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div><h2 class="text-sm font-medium text-gray-300">视频清单</h2><p class="mt-1 text-xs text-gray-500">点击视频查看刮削资料。新增或移动文件后，扫描目录更新清单。</p></div>
      <RouterLink :to="{ name: 'Config', query: { section: 'scrape' } }" class="text-xs text-brand">管理目录与刮削规则 ↗</RouterLink>
    </div>
    <details v-if="data.roots.length" class="text-xs text-gray-500"><summary class="cursor-pointer">已配置 {{ data.roots.length }} 个源目录 · 查看扫描状态</summary>
      <div v-for="item in data.roots" :key="item.path" class="mt-2 rounded bg-gray-800/40 px-3 py-2"><p class="break-all font-mono text-gray-400">{{ item.path }}</p><p class="mt-1">{{ item.count }} 个视频 · {{ item.last_scan ? `上次扫描 ${item.last_scan}` : '等待首次扫描' }}</p><p v-if="item.error" class="mt-1 text-amber-400">{{ item.error }} · 已保存的清单保留</p></div>
    </details>
    <p v-else class="text-xs text-gray-500">请先在设置 → 刮削 → 目录与命名中添加下载源目录。</p>
    <div v-if="inventory.running" class="rounded-lg bg-gray-800/40 p-3 text-xs text-brand">正在后台更新清单：已读取 {{ inventory.files }} 个视频，目录 {{ inventory.done }} / {{ inventory.total }}。可以继续查询已保存的视频。</div>
    <p v-if="inventory.errors?.length && !data.warnings.length" class="text-xs text-amber-400">{{ inventory.errors.join('；') }}</p>
    <form class="flex flex-wrap gap-2" @submit.prevent="search">
      <select v-model="root" class="input !w-auto max-w-full" aria-label="筛选源目录" @change="search"><option value="">全部下载源</option><option v-for="item in data.roots" :key="item.path" :value="item.path">{{ item.path }}</option></select>
      <input v-model="keyword" class="input min-w-36 flex-1" placeholder="搜索文件名、路径、番号或标题" aria-label="搜索源视频" />
      <select v-model="status" class="input !w-auto" aria-label="刮削状态筛选" @change="search"><option value="">全部状态</option><option v-for="(label, value) in LABELS" :key="value" :value="value">{{ label }}{{ data.counts[value] ? ` (${data.counts[value]})` : '' }}</option></select>
      <button class="btn-ghost" :disabled="loading">搜索</button><button type="button" class="btn-ghost" :disabled="inventory.running || !data.roots.length" @click="refreshIndex">{{ inventory.running ? '扫描中…' : '扫描目录更新清单' }}</button>
    </form>
    <div class="flex flex-wrap items-center gap-3 border-t border-gray-800 pt-3">
      <label class="flex cursor-pointer items-center gap-2 text-xs text-gray-400"><input type="checkbox" :checked="allSelected" aria-label="选择当前页可处理视频" @change="toggleAll" />全选当前页</label>
      <button class="btn-primary !px-3 !py-1.5 !text-xs" :disabled="!selectedRows.length || job.running" @click="open(selectedRows)">批量刮削{{ selectedRows.length ? ` · ${selectedRows.length}` : '' }}</button>
      <span class="ml-auto text-xs text-gray-500">{{ data.total }} 个视频<span v-if="keyword"> · 概况已按搜索条件筛选</span></span>
    </div>
    <p v-for="(warning, i) in data.warnings" :key="i" class="text-xs text-amber-400">{{ warning }}</p>
    <p v-if="loading" class="py-5 text-center text-sm text-gray-500">正在查询清单…</p>
    <div v-else-if="!data.items.length" class="rounded-xl border border-dashed border-gray-800 px-4 py-12 text-center"><p class="text-sm text-gray-400">{{ !data.roots.length ? '添加下载源，开始整理视频' : keyword || status ? '没有符合条件的视频' : '清单中还没有视频' }}</p><p class="mt-2 text-xs text-gray-600">{{ !data.roots.length ? '前往刮削设置，配置容器内可访问的下载目录。' : keyword || status ? '试试其他关键词，或切换到全部状态。' : '扫描下载目录后，视频会显示在这里。' }}</p><RouterLink v-if="!data.roots.length" :to="{ name: 'Config', query: { section: 'scrape' } }" class="mt-4 inline-block text-xs text-brand">配置下载源 ↗</RouterLink></div>
    <div v-else class="divide-y divide-gray-800/70">
      <article v-for="row in data.items" :key="row.path" class="group flex items-start gap-3 rounded-lg px-2 py-4 transition hover:bg-gray-800/30 sm:gap-4">
        <input v-model="selected" :value="row.path" type="checkbox" class="mt-2 shrink-0" :disabled="!selectable(row)" :aria-label="`选择 ${row.name}`" />
        <div class="min-w-0 flex-1">
          <button class="block max-w-full text-left focus-visible:outline focus-visible:outline-brand" :aria-label="`查看 ${row.name} 的刮削资料`" @click="showDetail(row)">
            <span class="flex flex-wrap items-center gap-2"><span class="font-mono text-sm font-medium text-brand">{{ row.code || '待识别' }}</span><span class="rounded border px-2 py-0.5 text-[10px]" :class="row.status === 'complete' ? 'border-emerald-500/20 bg-emerald-500/10 text-emerald-400' : ['failed', 'interrupted'].includes(row.status) ? 'border-red-500/20 bg-red-500/10 text-red-400' : 'border-gray-700 bg-gray-800 text-gray-400'">{{ LABELS[row.status] || row.status }}</span></span>
            <span class="mt-1.5 block break-all text-sm text-gray-300 group-hover:text-white">{{ row.title || row.name }}</span>
            <span v-if="row.title" class="mt-1 block break-all text-xs text-gray-500">{{ row.name }}</span>
          </button>
          <p class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-gray-500"><span>{{ formatSize(row.size) }}</span><span v-if="row.part">分集 {{ row.part }}</span><span v-if="row.last_run">上次处理 {{ row.last_run }}</span></p>
          <p v-if="row.error" class="mt-2 break-all text-xs text-red-400">{{ row.error }}</p>
        </div>
        <div class="flex shrink-0 flex-col items-end gap-3 pt-1 sm:flex-row sm:items-center">
          <button class="text-xs text-gray-400 hover:text-brand" @click="showDetail(row)">查看资料</button>
          <button class="text-xs text-brand disabled:text-gray-600" :disabled="!selectable(row) || job.running" @click="open([row], !!row.links.length)">{{ !row.code ? '指定番号' : row.links.length ? '再次刮削' : '手动刮削' }}</button>
        </div>
      </article>
    </div>
    <div v-if="data.total > data.size" class="flex items-center justify-center gap-4"><button class="btn-ghost" :disabled="page <= 1 || loading" @click="changePage(-1)">上一页</button><span class="text-xs text-gray-500">{{ page }} / {{ Math.max(1, Math.ceil(data.total / data.size)) }}</span><button class="btn-ghost" :disabled="page * data.size >= data.total || loading" @click="changePage(1)">下一页</button></div>
    </div>
    <ScrapeMetadataDetail v-model:open="detailOpen" :row="detailRow" :status-label="LABELS[detailRow?.status] || detailRow?.status || ''" allow-process :process-disabled="!detailRow || !selectable(detailRow) || job.running" @process="processDetail" />
    <ModalDialog v-model:open="dialog" title="指定番号与手动刮削" size="2xl" scrollable>
      <div class="space-y-4">
        <p class="text-xs text-gray-500">每个文件使用自己的番号，同番号的分集会合并处理。请确认视频已下载完成；已有入库关联时优先更新原位置。此操作不删除源视频。</p>
        <div v-for="row in drafts" :key="row.path" class="space-y-2 rounded-lg bg-gray-800/50 p-3"><p class="break-all text-xs text-gray-300">{{ row.name }}</p><div class="flex flex-wrap items-center gap-2"><input v-model="row.code" class="input !w-44 font-mono" placeholder="如 SONE-725" :aria-label="`${row.name} 的指定番号`" maxlength="128" /><button class="text-xs text-brand" :disabled="!row.code.trim() || submitting" @click="preview(row)">用此番号试算路径</button></div></div>
        <details class="space-y-3 rounded-lg border border-gray-700 p-3"><summary class="cursor-pointer text-sm text-gray-400">本次任务选项 · {{ options.overwrite ? '覆盖 NFO/图片' : '仅补缺失产物' }}</summary>
        <label class="flex items-center gap-2 text-sm text-gray-400"><input v-model="options.fetch_meta" type="checkbox" @change="toggleFetch" />联网补全缺失资料</label>
        <label class="flex items-center gap-2 text-sm text-gray-400"><input v-model="options.refresh_metadata" type="checkbox" :disabled="!options.fetch_meta" />重新获取元数据</label>
        <p class="text-[11px] text-gray-500">更新本轮返回的字段，未返回字段保留。</p>
        <label class="flex items-center gap-2 text-sm text-gray-400"><input v-model="options.overwrite" type="checkbox" />覆盖已有 NFO 和图片</label><p class="text-[11px] text-gray-500">再次刮削默认开启。本次选项不修改设置页中的自动任务规则。</p>
        </details>
        <button class="btn-primary" :disabled="submitting || job.running || !drafts.length || drafts.some(r => !r.code.trim())" @click="start">{{ submitting ? '加入任务中…' : `开始处理 ${drafts.length} 个视频` }}</button>
      </div>
    </ModalDialog>
  </section>
</template>
