<script setup>
import PagePanel from '@/components/PagePanel.vue'
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import http from '@/api'
import ModalDialog from '@/components/ModalDialog.vue'
import { useToast } from '@/composables/useToast'
const route = useRoute(), router = useRouter(), toast = useToast()
const data = ref({ items: [], counts: {}, total: 0, errors: [], clients: [] })
const transfer = ref({ current: { running: false, results: [] }, history: [], available: false })
const busy = ref(false), error = ref(''), transferError = ref(''), keyword = ref(''), client = ref('')
const chosen = ref([]), detail = ref(null), confirm = ref(false), deleteSource = ref(false), submitting = ref(false)
let revision = 0, timer, disposed = false
const tab = computed(() => route.query.tab === 'history' ? 'history' : 'live')
const state = computed(() => route.query.state || '')
const page = computed(() => Math.max(1, Number(route.query.page) || 1))
const codeStatus = computed(() => /^[0-5]$/.test(String(route.query.code_status ?? '')) ? Number(route.query.code_status) : undefined)
const STATES = [['', '全部任务'], ['downloading', '下载中'], ['seeding', '做种中'], ['paused', '已暂停'], ['checking', '校验中'], ['error', '异常']]
const CODE_STATES = ['未订阅', '待搜种', '下载中', '已下载', '已入库', '失败']
const labels = Object.fromEntries(STATES)
const errorLabels = { tracker_warning: 'Tracker 警告', tracker_error: 'Tracker 错误', local_error: '任务本地错误', unknown: '下载器错误' }
const taskError = row => row.error_message || (row.group === 'error' ? '下载器未提供详细原因，请到下载器查看任务错误。' : '')
const names = { qbittorrent: 'qBittorrent', transmission: 'Transmission', thunder: '迅雷' }
const current = computed(() => transfer.value.current || { running: false, results: [] })
const elapsed = computed(() => { const job=current.value; if (!job.started_at) return ''; const seconds=Math.max(0,Math.floor((job.running ? Date.now()/1000 : job.updated_at || job.started_at)-job.started_at)); return `${Math.floor(seconds/60)} 分 ${seconds%60} 秒` })
const successes = job => (job.results || []).filter(row => row.status === 'succeeded').length
const failures = job => (job.results || []).filter(row => row.status === 'failed').length
const eligible = row => row.client === 'qbittorrent' && row.progress >= 1 && !row.stale && !['error', 'checking'].includes(row.group) && !/moving|allocating/i.test(row.state)
const selectedRows = computed(() => data.value.items.filter(row => chosen.value.includes(row.hash) && eligible(row)))
const size = value => value == null || value < 0 ? '—' : value >= 1073741824 ? `${(value / 1073741824).toFixed(2)} GB` : value >= 1048576 ? `${(value / 1048576).toFixed(1)} MB` : `${Math.round(value / 1024)} KB`
const speed = value => value == null ? '—' : `${size(value)}/s`
const count = value => value == null || value < 0 ? '—' : value
const progress = row => row.progress == null ? '未知' : `${Math.min(100, Math.max(0, row.progress * 100)).toFixed(1)}%`
const date = value => !value || value < 0 ? '—' : new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
const eta = value => value == null || value < 0 || value >= 8640000 ? '未知' : value >= 3600 ? `${Math.floor(value / 3600)} 小时 ${Math.floor(value % 3600 / 60)} 分` : `${Math.floor(value / 60)} 分`

function navigate(changes = {}) { router.replace({ query: { ...route.query, ...changes, page: changes.page || undefined } }) }
function search() { navigate({ keyword: keyword.value.trim() || undefined, client: client.value || undefined }) }
function switchTab(value) { navigate({ tab: value === 'history' ? 'history' : undefined, state: undefined, code_status: undefined }) }
function chooseState(value) { navigate({ tab: undefined, state: value || undefined }) }
function toggleAll() { const hashes = data.value.items.filter(eligible).map(row => row.hash); chosen.value = hashes.length && hashes.every(hash => chosen.value.includes(hash)) ? [] : hashes }
async function refresh() {
  clearTimeout(timer)
  const id = ++revision
  busy.value = true
  const params = { keyword: keyword.value.trim(), client: client.value, state: state.value, code_status: codeStatus.value, page: page.value, size: 30 }
  const results = await Promise.allSettled([http.get(tab.value === 'history' ? '/downloads/history' : '/downloads', { params }), http.get('/downloads/transfer')])
  if (disposed || id !== revision) return
  if (results[0].status === 'fulfilled') {
    data.value = results[0].value; error.value = ''
    chosen.value = chosen.value.filter(hash => data.value.items.some(row => row.hash === hash && eligible(row)))
  } else error.value = results[0].reason.message
  if (results[1].status === 'fulfilled') { transfer.value = results[1].value; transferError.value = '' }
  else transferError.value = results[1].reason.message
  busy.value = false
  timer = setTimeout(refresh, current.value.running ? 2500 : 10000)
}
function openTransfer() { if (!selectedRows.value.length) return; deleteSource.value = false; confirm.value = true }
async function start(resume = false) {
  submitting.value = true
  try {
    const result = await http.post('/downloads/transfer', resume ? { resume: true } : { hashes: selectedRows.value.map(row => row.hash), delete_source: deleteSource.value })
    transfer.value.current = result; confirm.value = false; chosen.value = []
    toast.success('转移做种已在后台启动'); await refresh()
  } catch (err) { toast.error(err.message) }
  finally { submitting.value = false }
}
watch(() => route.query, () => {
  keyword.value = String(route.query.keyword || ''); client.value = String(route.query.client || '')
  chosen.value = []; data.value = { items: [], counts: {}, errors: [], clients: data.value.clients || [], total: 0 }
  refresh()
}, { immediate: true })
onUnmounted(() => { disposed = true; revision++; clearTimeout(timer) })
</script>

<template>
  <PagePanel content-class="space-y-2 sm:space-y-5" :mobile-summary="`${data.total || 0} 个任务 · ${state ? labels[state] || state : '全部状态'}`">
    <template #navigation>
    <div class="flex gap-2"><button class="btn-ghost" :class="tab === 'live' ? 'text-brand' : ''" @click="switchTab('live')">实时任务</button><button class="btn-ghost" :class="tab === 'history' ? 'text-brand' : ''" @click="switchTab('history')">下载历史</button></div>
    </template>
    <template #header>

    <div class="flex flex-wrap items-center justify-between gap-3"><div class="mobile-page-intro hidden sm:block"><h1 class="text-xl font-semibold">下载管理</h1><p class="mt-1 text-sm text-gray-500">查看下载与做种状态、下载历史，并转移到 Transmission 长期做种。</p></div><button class="mobile-page-intro btn-ghost hidden sm:inline-flex" :disabled="busy" @click="refresh">{{ busy ? '刷新中…' : '刷新状态' }}</button></div>

    <div v-if="tab === 'live'" class="mobile-stat-grid grid grid-cols-2 gap-3 lg:grid-cols-6"><button v-for="[key,label] in STATES" :key="key" class="card text-left transition hover:border-brand/60" :class="state === key ? 'border-brand/50' : ''" @click="chooseState(key)"><p class="text-xs text-gray-500">{{ label }}</p><p class="mt-2 text-xl tabular-nums">{{ data.counts?.[key || 'all'] ?? '—' }}</p></button></div>
    <div v-if="tab === 'live'" class="flex flex-wrap gap-4 text-sm text-gray-400"><span>下载 ↓ {{ speed(data.download_speed) }}</span><span>上传 ↑ {{ speed(data.upload_speed) }}</span><span v-if="data.updated_at">快照 {{ date(data.updated_at) }}</span></div>
    <p v-if="tab === 'history'" class="text-xs text-gray-500">保留系统推送记录和此页读取过的任务快照，下载器移除任务后仍可查看。历史进度为最后一次观测值，未记录的旧任务不推算完成时间。</p>
    <p v-if="codeStatus !== undefined" class="text-sm text-brand">按系统影片状态筛选：{{ CODE_STATES[codeStatus] }}。下载器当前状态与速度见各行。<button class="ml-3 underline" @click="navigate({ code_status: undefined })">查看全部任务</button></p>
    <form class="flex flex-wrap gap-3" @submit.prevent="search"><input v-model="keyword" class="input min-w-40 flex-1" aria-label="搜索下载任务" placeholder="搜索名称、番号、hash 或存储路径"><select v-model="client" class="input !w-auto" aria-label="选择下载器" @change="search"><option value="">全部下载器</option><option v-for="name in ['qbittorrent','transmission','thunder']" :key="name" :value="name">{{ names[name] }}</option></select><button class="btn-ghost">搜索</button></form>
    </template>

    <p v-if="error" class="text-sm text-red-400">读取失败：{{ error }}。已显示的数据可能不是最新状态。</p>
    <p v-for="item in data.errors || []" :key="item.client" class="text-sm text-amber-400">{{ names[item.client] || item.client }}：{{ item.message }}；旧快照不计入实时速度。</p>
    <p v-if="tab === 'live' && !busy && !data.clients?.length && !error" class="text-xs text-amber-400">尚未配置可用下载器，请在设置中填写 qBittorrent 或 Transmission 连接。</p>
    <p v-if="!transfer.available && transfer.reason" class="text-xs text-gray-500">转移做种：{{ transfer.reason }}。<RouterLink :to="{name:'Config',query:{section:'downloader'}}" class="text-brand">配置下载器 ↗</RouterLink></p>
    <div v-if="tab === 'live'" class="flex flex-wrap items-center gap-2 text-xs"><button class="text-brand" @click="toggleAll">选择本页可转移任务</button><span class="text-gray-500">已选 {{ selectedRows.length }} 个</span><button class="btn-primary !text-xs" :disabled="!selectedRows.length || !transfer.available || current.running || submitting || !!transferError" @click="openTransfer">转移做种</button><span class="hidden text-gray-500 sm:inline">仅 qB 已完成且文件就位的任务可选。</span></div>
    <p v-if="busy && !data.items.length" class="py-10 text-center text-gray-500">正在读取下载器…</p>
    <p v-else-if="!data.items.length" class="card py-10 text-center text-gray-500">{{ error || data.errors?.length ? '暂时无法取得匹配的任务，请检查连接后刷新。' : '暂无匹配任务或记录。' }}</p>
    <div v-else class="space-y-2 sm:space-y-3"><article v-for="row in data.items" :key="row.key" class="card space-y-1.5 sm:space-y-3">
      <div class="flex items-start gap-3"><input v-if="tab === 'live'" v-model="chosen" type="checkbox" :value="row.hash" :disabled="!eligible(row) || current.running" :aria-label="`选择转移 ${row.name}`" class="mt-1"><div class="min-w-0 flex-1"><button class="line-clamp-2 break-all text-left text-sm font-medium text-gray-200 hover:text-brand" @click="detail = row">{{ row.name }}</button><p class="mt-1 text-xs text-gray-500">{{ names[row.client] || '旧系统记录 · 下载器未知' }} · {{ labels[row.group] || row.state }} <span v-if="row.stale" class="text-amber-400"> · 旧快照 {{ date(row.snapshot_at) }}</span><span v-if="row.code"> · {{ row.code }}</span><span v-if="row.site"> · {{ row.site }}</span></p></div><button class="shrink-0 text-xs text-brand" @click="detail = row">详情</button></div>
      <p v-if="taskError(row)" class="break-all rounded-lg bg-black/20 p-2 text-xs" :class="row.error_kind?.startsWith('tracker_') ? 'text-amber-300' : 'text-red-400'">{{ errorLabels[row.error_kind] || '任务异常' }}：{{ taskError(row) }}<span v-if="row.error_kind?.startsWith('tracker_')" class="ml-2 text-gray-400">Tracker 报错不代表视频文件丢失。</span></p>
      <div class="grid grid-cols-2 gap-3 text-xs text-gray-400 sm:grid-cols-4 lg:grid-cols-6"><div>进度 <span class="text-gray-200">{{ progress(row) }}</span><span class="ml-2">{{ size(row.size) }}</span></div><div v-if="tab === 'live'">↓ {{ row.stale ? '—' : speed(row.download_speed) }} · ↑ {{ row.stale ? '—' : speed(row.upload_speed) }}</div><div v-if="tab === 'live'" class="hidden sm:block">做种人数 {{ count(row.seeds_total) }} <span v-if="row.seeds_connected != null">（已连接 {{ count(row.seeds_connected) }}）</span></div><div v-if="tab === 'live'" class="hidden sm:block">连接 peers {{ count(row.peers_connected) }}</div><div v-if="tab === 'live'" class="hidden sm:block">预计剩余 {{ eta(row.eta) }}</div><div v-if="tab === 'live'" class="hidden sm:block">分享率 {{ row.ratio == null || row.ratio < 0 ? '—' : row.ratio.toFixed(2) }}</div><div v-if="tab === 'history'">记录于 {{ date(row.recorded_at) }}</div><div v-if="row.completed_at > 0">完成 {{ date(row.completed_at) }}</div></div>
      <div v-if="row.progress != null" class="h-1 overflow-hidden rounded-full bg-gray-800"><div class="h-full bg-brand" :style="{width:`${Math.min(100,Math.max(0,row.progress*100))}%`}"></div></div><p class="hidden break-all font-mono text-xs text-gray-500 sm:block">{{ row.save_path || '尚未记录存储路径' }}</p>
    </article></div>
    <div v-if="data.total > 30" class="flex justify-center gap-4 text-xs"><button class="btn-ghost" :disabled="page <= 1 || busy" @click="navigate({page:String(page-1)})">上一页</button><span class="self-center">{{ page }} / {{ Math.ceil(data.total/30) }} · {{ data.total }} 个任务</span><button class="btn-ghost" :disabled="page*30 >= data.total || busy" @click="navigate({page:String(page+1)})">下一页</button></div>
    <section class="card space-y-3" aria-label="转移做种任务"><h2 class="font-medium">转移做种记录</h2><p v-if="transferError" class="text-sm text-red-400">{{ transferError }}</p><template v-if="current.job_id"><p class="text-sm text-gray-300">{{ current.running ? '后台转移中' : ({completed:'转移完成',partial:'部分失败',interrupted:'已中断',failed:'任务失败'})[current.phase] || current.phase }} · {{ current.results?.length || 0 }} / {{ current.hashes?.length || 0 }} · 成功 {{ successes(current) }} · 失败 {{ failures(current) }}</p><p v-if="current.current" class="break-all text-xs text-brand">正在处理 {{ current.current }}</p><p v-if="current.updated_at" class="text-xs text-gray-500">耗时 {{ elapsed }} · 最后进度 {{ date(current.updated_at) }} · 可以离开此页，任务在后台继续</p><p v-if="current.error" class="text-xs text-red-400">{{ current.error }}</p><button v-if="['partial','failed','interrupted'].includes(current.phase)" class="btn-primary !text-xs" :disabled="submitting || current.running || !!transferError" @click="start(true)">继续未完成 / 重试失败</button></template><p v-else class="text-xs text-gray-500">暂无手动转移记录。自动转移仍按设置中的周期执行。</p><details v-for="job in transfer.history" :key="job.job_id" class="border-t border-gray-800 pt-3"><summary class="cursor-pointer text-xs text-gray-400">{{ date(job.started_at) }} · {{ job.phase }} · 成功 {{ successes(job) }} / {{ job.hashes?.length || 0 }} · 查看结果</summary><p v-for="row in job.results || []" :key="row.hash" class="mt-2 break-all text-xs" :class="row.status === 'failed' ? 'text-red-400' : 'text-gray-500'">{{ row.hash }} · {{ {succeeded:'已转移',skipped:'未满足转移条件，已跳过',failed:'失败'}[row.status] }} {{ row.error }}</p></details></section>
    <ModalDialog v-model:open="confirm" title="转移到 Transmission 做种" :close-on-backdrop="!submitting" :close-on-esc="!submitting"><div class="space-y-4"><p class="text-sm text-gray-300">将所选 {{ selectedRows.length }} 个 qB 已完成任务交给 Transmission。使用已有文件并校验，按设置映射存储路径。</p><p v-for="row in selectedRows" :key="row.hash" class="break-all text-xs text-gray-500">{{ row.name }} · {{ row.save_path }}</p><label class="flex gap-2 text-sm"><input v-model="deleteSource" type="checkbox" :disabled="submitting">接管后移除 qB 源任务（保留视频文件）</label><div class="flex justify-end gap-3"><button class="btn-ghost" :disabled="submitting" @click="confirm=false">取消</button><button class="btn-primary" :disabled="submitting || !selectedRows.length || current.running" @click="start(false)">{{ submitting ? '启动中…' : '确认转移做种' }}</button></div></div></ModalDialog>
    <ModalDialog :open="!!detail" :title="detail?.name || '下载详情'" scrollable @update:open="value => {if(!value) detail=null}"><div v-if="detail" class="space-y-3 text-sm text-gray-400"><p>{{ names[detail.client] || '历史记录' }} · {{ detail.state }} · {{ progress(detail) }}</p><p v-if="taskError(detail)" class="break-all text-amber-300">{{ errorLabels[detail.error_kind] || '任务异常' }}：{{ taskError(detail) }}</p><p class="break-all">Hash：{{ detail.hash }}</p><p class="break-all">存储目录：{{ detail.save_path || '未记录' }}</p><p v-if="detail.content_path" class="break-all">内容路径：{{ detail.content_path }}</p><p>大小 {{ size(detail.size) }} · 累计下载 {{ size(detail.downloaded) }} · 累计上传 {{ size(detail.uploaded) }}</p><p>做种人数 {{ count(detail.seeds_total) }} · Peers {{ count(detail.peers_connected) }} · 预计剩余 {{ eta(detail.eta) }} · 分享率 {{ detail.ratio == null ? '—' : detail.ratio }}</p><p>添加 {{ date(detail.added_at) }} · 完成 {{ date(detail.completed_at) }}</p><p v-if="detail.last_seen">最后观测 {{ date(detail.last_seen) }}</p><p v-if="detail.code_status != null">系统影片状态：{{ CODE_STATES[detail.code_status] }}</p><RouterLink v-if="detail.code" :to="{name:'Search',query:{keyword:detail.code}}" class="text-brand">查看影片 {{ detail.code }} ↗</RouterLink></div></ModalDialog>
  </PagePanel>
</template>
