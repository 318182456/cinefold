<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import http from '@/api'
import { useConfigStore } from '@/stores/config'
import { useToast } from '@/composables/useToast'
import ModalDialog from '@/components/ModalDialog.vue'

const emit = defineEmits(['preview'])
const toast = useToast(), configStore = useConfigStore()
const directory = ref(''), adding = ref(false), loading = ref(false)
const data = ref({ roots: [], items: [], total: 0, size: 30, counts: {}, warnings: [] })
const root = ref(''), keyword = ref(''), status = ref(''), page = ref(1), selected = ref([])
const job = ref({ running: false, results: [], total: 0, done: 0, ok: 0 })
const dialog = ref(false), drafts = ref([]), submitting = ref(false)
const options = ref({ overwrite: false, refresh_metadata: false, fetch_meta: true })
const LABELS = { pending: '未刮削', complete: '已刮削', incomplete: '产物缺失', failed: '刮削失败', unrecognized: '需指定番号', downloading: '下载中', queued: '排队中', running: '刮削中', interrupted: '上次中断', cancelled: '已停止', empty: '空文件' }
let request = 0, timer, disposed = false
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
    selected.value = selected.value.filter(path => result.items.some(r => r.path === path && selectable(r)))
    if (!result.items.length && page.value > 1 && result.total > 0) { page.value = Math.ceil(result.total / result.size); await load() }
  } catch (e) { if (!disposed && id === request) toast.error(e.message) } finally { if (id === request) loading.value = false }
}
function search() { page.value = 1; selected.value = []; load() }
async function add() {
  if (!directory.value.trim()) return
  adding.value = true
  try {
    await http.post('/scrape/sources', { path: directory.value.trim() })
    directory.value = ''; await configStore.load(true); await load(); toast.success('已添加下载源目录')
  } catch (e) { toast.error(e.message) } finally { adding.value = false }
}
async function remove(path) {
  try {
    await http.delete('/scrape/sources', { params: { path } })
    if (root.value === path) root.value = ''
    await configStore.load(true); search(); toast.success('已移除目录配置，文件保留')
  } catch (e) { toast.error(e.message) }
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
onMounted(() => { load(); poll() })
onUnmounted(() => { disposed = true; request++; clearTimeout(timer) })
</script>

<template>
  <section class="card space-y-4">
    <div><h2 class="text-sm font-medium text-gray-300">下载源视频</h2><p class="mt-1 text-xs text-gray-500">添加下载源目录后，可查看视频、指定番号并手动刮削。文件和原入库视频会保留；自动扫描需在设置中启用 builtin 模式。</p></div>
    <form class="flex flex-wrap gap-2" @submit.prevent="add"><input v-model="directory" class="input min-w-48 flex-1 font-mono text-xs" placeholder="容器内目录，如 /volume3/h_video/Download/日本AV" aria-label="添加下载源目录" /><button class="btn-ghost" :disabled="adding || job.running">{{ adding ? '添加中…' : '添加目录' }}</button></form>
    <div v-if="data.roots.length" class="space-y-1.5">
      <div v-for="item in data.roots" :key="item.path" class="flex items-start gap-2 rounded bg-gray-800/40 px-3 py-2 text-xs">
        <div class="min-w-0 flex-1"><p class="break-all font-mono" :class="item.available ? 'text-gray-400' : 'text-amber-400'">{{ item.path }}</p><p v-if="item.error" class="mt-1 text-amber-400">{{ item.error }}</p></div>
        <button class="shrink-0 text-gray-500 hover:text-red-400 disabled:opacity-30" :disabled="job.running" :aria-label="`移除监控目录 ${item.path}，保留文件`" @click="remove(item.path)">移除配置</button>
      </div>
    </div>
    <p v-else class="text-xs text-gray-500">尚未配置下载源目录。也可继续使用下方路径试算。</p>
    <form class="flex flex-wrap gap-2" @submit.prevent="search">
      <select v-model="root" class="input !w-auto max-w-full" aria-label="筛选源目录" @change="search"><option value="">全部下载源</option><option v-for="item in data.roots" :key="item.path" :value="item.path">{{ item.path }}</option></select>
      <input v-model="keyword" class="input min-w-36 flex-1" placeholder="搜索文件名、路径、番号或标题" aria-label="搜索源视频" />
      <select v-model="status" class="input !w-auto" aria-label="刮削状态筛选" @change="search"><option value="">全部状态</option><option v-for="(label, value) in LABELS" :key="value" :value="value">{{ label }}{{ data.counts[value] ? ` (${data.counts[value]})` : '' }}</option></select>
      <button class="btn-ghost" :disabled="loading">搜索</button><button type="button" class="btn-ghost" :disabled="loading" @click="load(); poll()">刷新列表</button>
    </form>
    <div class="flex flex-wrap items-center gap-2"><button class="btn-ghost" :disabled="!selectedRows.length || job.running" @click="open(selectedRows)">刮削选中（{{ selectedRows.length }}）</button><button class="btn-ghost" :disabled="!selectedRows.length || job.running" @click="open(selectedRows, true)">再次刮削选中</button><span class="ml-auto text-xs text-gray-500">{{ data.total }} 个视频 · 仅选择当前页</span></div>
    <p v-for="(warning, i) in data.warnings" :key="i" class="text-xs text-amber-400">{{ warning }}</p>
    <p v-if="loading" class="py-5 text-center text-sm text-gray-500">正在读取目录与视频状态…</p>
    <div v-else-if="!data.items.length" class="py-6 text-center text-sm text-gray-500">没有符合条件的视频</div>
    <div v-else class="overflow-x-auto">
      <table class="w-full min-w-[720px] text-left text-xs"><thead class="text-gray-500"><tr class="border-b border-gray-800"><th class="w-8 py-2"><input type="checkbox" :checked="allSelected" aria-label="选择当前页可处理视频" @change="toggleAll" /></th><th class="py-2">视频</th><th>番号 / 状态</th><th>大小</th><th class="text-right">操作</th></tr></thead>
        <tbody><tr v-for="row in data.items" :key="row.path" class="border-b border-gray-800/60 align-top"><td class="py-3"><input v-model="selected" :value="row.path" type="checkbox" :disabled="!selectable(row)" :aria-label="`选择 ${row.name}`" /></td><td class="max-w-sm py-3 pr-3"><p class="break-all text-gray-300">{{ row.name }}</p><p v-if="row.title" class="mt-1 line-clamp-1 text-gray-500">{{ row.title }}</p><p class="mt-1 break-all text-[10px] text-gray-600">{{ row.path }}</p><p v-if="row.error" class="mt-1 break-all text-red-400">{{ row.error }}</p><p v-if="row.links.length" class="mt-1 break-all text-[10px] text-gray-500">入库：{{ row.links[0] }}</p></td><td class="py-3 pr-3"><p class="font-mono text-brand">{{ row.code || '未识别' }}</p><p class="mt-1" :class="row.status === 'complete' ? 'text-emerald-400' : ['failed', 'interrupted'].includes(row.status) ? 'text-red-400' : 'text-gray-500'">{{ LABELS[row.status] || row.status }}</p><p v-if="row.last_run" class="mt-1 text-[10px] text-gray-600">{{ row.last_run }}</p></td><td class="whitespace-nowrap py-3 pr-3 text-gray-500">{{ formatSize(row.size) }}</td><td class="py-3 text-right"><div class="flex justify-end gap-3 whitespace-nowrap"><button class="text-gray-400 hover:text-brand" @click="open([row], !!row.links.length)">指定番号</button><button class="text-brand disabled:text-gray-600" :disabled="!selectable(row) || job.running" @click="open([row], !!row.links.length)">{{ row.links.length ? '再次刮削' : '手动刮削' }}</button></div></td></tr></tbody>
      </table>
    </div>
    <div v-if="data.total > data.size" class="flex items-center justify-center gap-4"><button class="btn-ghost" :disabled="page <= 1 || loading" @click="changePage(-1)">上一页</button><span class="text-xs text-gray-500">{{ page }} / {{ Math.max(1, Math.ceil(data.total / data.size)) }}</span><button class="btn-ghost" :disabled="page * data.size >= data.total || loading" @click="changePage(1)">下一页</button></div>
    <div v-if="job.job_id" class="space-y-2 rounded-lg border border-gray-700 p-3">
      <div class="flex flex-wrap items-center gap-3"><p class="text-sm text-gray-300">{{ job.running ? '后台刮削中' : job.cancel_requested ? '后续任务已停止' : '最近一次手动刮削' }} · {{ job.done }} / {{ job.total }} 个番号 · 成功 {{ job.ok }} 个</p><button v-if="job.running" class="text-xs text-amber-400 disabled:opacity-40" :disabled="job.cancel_requested" @click="cancel">{{ job.cancel_requested ? '当前番号完成后停止' : '停止后续任务' }}</button></div>
      <p v-if="job.current" class="text-xs text-brand">当前处理 {{ job.current }}，可离开页面，任务会继续执行。</p><p v-if="job.error" class="text-xs text-red-400">{{ job.error }}</p>
      <details v-if="job.results.length"><summary class="cursor-pointer text-xs text-gray-400">查看逐项结果</summary><div v-for="(row, i) in job.results" :key="i" class="mt-2 border-t border-gray-800 pt-2 text-xs"><p :class="row.ok ? 'text-emerald-400' : 'text-red-400'">{{ row.code }} · {{ row.ok ? '完成' : '失败' }} · NFO {{ row.nfo_written }} · 图片 {{ row.images_written }}</p><p v-if="row.error" class="mt-1 break-all text-red-400">{{ row.error }}</p><p v-for="(warning, n) in row.warnings" :key="n" class="mt-1 text-amber-400">{{ warning }}</p><p v-for="path in row.links" :key="path" class="mt-1 break-all text-gray-500">{{ path }}</p></div></details>
    </div>
    <ModalDialog v-model:open="dialog" title="指定番号与手动刮削" size="2xl" scrollable>
      <div class="space-y-4">
        <p class="text-xs text-gray-500">每个文件使用自己的番号，同番号的分集会合并处理。请确认视频已下载完成；已有入库关联时优先更新原位置。此操作不删除源视频。</p>
        <div v-for="row in drafts" :key="row.path" class="space-y-2 rounded-lg bg-gray-800/50 p-3"><p class="break-all text-xs text-gray-300">{{ row.name }}</p><div class="flex flex-wrap items-center gap-2"><input v-model="row.code" class="input !w-44 font-mono" placeholder="如 SONE-725" :aria-label="`${row.name} 的指定番号`" maxlength="128" /><button class="text-xs text-brand" :disabled="!row.code.trim() || submitting" @click="preview(row)">用此番号试算路径</button></div></div>
        <label class="flex items-center gap-2 text-sm text-gray-400"><input v-model="options.fetch_meta" type="checkbox" @change="toggleFetch" />联网补全缺失资料</label>
        <label class="flex items-center gap-2 text-sm text-gray-400"><input v-model="options.refresh_metadata" type="checkbox" :disabled="!options.fetch_meta" />重新联网获取元数据</label><p class="text-[11px] text-gray-500">开启后更新各源本轮返回的字段；未返回字段与已有译文保留。订阅、收藏与播放记录保留。</p>
        <label class="flex items-center gap-2 text-sm text-gray-400"><input v-model="options.overwrite" type="checkbox" />覆盖已有 NFO 和图片</label><p class="text-[11px] text-gray-500">再次刮削默认开启，手动修改过的 NFO 与海报可能被替换；关闭则只补缺失产物。本次选择不会改变全局覆盖设置。</p>
        <button class="btn-primary" :disabled="submitting || job.running || !drafts.length || drafts.some(r => !r.code.trim())" @click="start">{{ submitting ? '加入任务中…' : `开始处理 ${drafts.length} 个视频` }}</button>
      </div>
    </ModalDialog>
  </section>
</template>
