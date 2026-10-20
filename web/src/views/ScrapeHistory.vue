<script setup>
import PagePanel from '@/components/PagePanel.vue'
import { onMounted, onUnmounted, ref } from 'vue'
import http from '@/api'
import ModalDialog from '@/components/ModalDialog.vue'
import { revealDetails } from '@/utils/statNavigation'
import { useToast } from '@/composables/useToast'

const toast = useToast()
const data = ref({ items: [], total: 0 }), page = ref(1), loading = ref(false), error = ref('')
const selected = ref(''), detail = ref(null), detailPage = ref(1), status = ref(''), detailBusy = ref(false)
const busy = ref(false), deleting = ref(null)
let timer, disposed = false, revision = 0
const phases = { completed: '已完成', partial: '部分失败', interrupted: '已中断', failed: '任务失败', cancelled: '已停止', running: '刮削中', checking: '检查中', quarantine: '备份中', backup: '备份中', clearing: '清理中', rebuilding: '刮削中' }
const kinds = { rebuild: '全部重新刮削', manual: '手动批量刮削', automatic: '自动刮削' }
const date = value => value ? new Date(value * 1000).toLocaleString() : '—'

async function load() {
  clearTimeout(timer)
  loading.value = true
  try {
    const result = await http.get('/scrape/history', { params: { page: page.value, size: 20 } })
    if (disposed) return
    data.value = result
    error.value = ''
    if (selected.value) await openDetail(selected.value)
  } catch (err) { if (!disposed) error.value = err.message }
  finally {
    loading.value = false
    if (!disposed) timer = setTimeout(load, 5000)
  }
}
async function openDetail(id, reset = false) {
  if (reset) { detailPage.value = 1; status.value = ''; detail.value = null }
  selected.value = id
  const request = ++revision
  detailBusy.value = true
  try {
    const result = await http.get(`/scrape/history/${id}`, { params: { page: detailPage.value, size: 30, status: status.value } })
    if (!disposed && request === revision) detail.value = result
  } catch (err) { if (!disposed && request === revision) toast.error(err.message) }
  finally { if (request === revision) detailBusy.value = false }
}
async function showResults(id, filter = '') {
  detailPage.value = 1; status.value = filter; detail.value = null
  const loading = openDetail(id)
  revealDetails('scrape-history-results')
  await loading
}
async function resume(row, mode) {
  busy.value = true
  try {
    await http.post(`/scrape/history/${row.job_id}/resume`, { mode })
    toast.success(mode === 'failed' ? '已开始重试失败项' : '已开始继续任务')
    await load()
  } catch (err) { toast.error(err.message) }
  finally { busy.value = false }
}
async function remove() {
  busy.value = true
  try {
    await http.delete(`/scrape/history/${deleting.value.job_id}`)
    if (selected.value === deleting.value.job_id) { selected.value = ''; detail.value = null; revision++ }
    deleting.value = null
    toast.success('记录已删除，影片和备份保留')
    if (data.value.items.length === 1 && page.value > 1) page.value--
    await load()
  } catch (err) { toast.error(err.message) }
  finally { busy.value = false }
}
onMounted(load)
onUnmounted(() => { disposed = true; revision++; clearTimeout(timer) })
</script>

<template>
  <PagePanel content-class="space-y-4">
    <template #header>

    <div class="flex items-start justify-between gap-4">
      <div><h2 class="font-medium text-gray-200">刮削记录 <span class="ml-2 text-xs text-gray-500">{{ data.total }} 个任务</span></h2><p class="mt-1 text-xs text-gray-500">保存自动刮削、手动批量刮削与全部重建结果。继续任务保留已完成影片；删除记录保留影片、下载源和备份。</p></div>
      <button class="btn-ghost shrink-0" :disabled="loading || busy" @click="load">刷新</button>
    </div>
    </template>

    <p v-if="error" class="text-sm text-red-400" role="alert">{{ error }}</p>
    <p v-if="!data.items.length" class="card text-sm text-gray-500">{{ loading ? '正在读取记录…' : '暂无刮削记录' }}</p>
    <article v-for="row in data.items" :key="row.job_id" class="card space-y-3">
      <div class="flex flex-wrap justify-between gap-3">
        <div><p class="text-sm font-medium text-gray-200">{{ kinds[row.kind] || row.kind }} <span class="ml-2 text-xs" :class="row.running ? 'text-brand' : row.failed ? 'text-amber-400' : 'text-gray-400'">{{ phases[row.phase] || row.phase }}</span></p><p class="mt-1 text-xs text-gray-500">{{ date(row.started_at) }} · {{ row.job_id.slice(0, 8) }} <span v-if="row.parent_id">· 续接 {{ row.parent_id.slice(0, 8) }}</span></p></div>
        <div class="flex flex-wrap items-center gap-2 text-xs">
          <button class="btn-ghost !text-xs" @click="openDetail(row.job_id, true)">查看明细</button>
          <button v-if="row.can_resume" class="btn-primary !text-xs" :disabled="busy || data.running" @click="resume(row, 'unfinished')">继续未完成</button>
          <button v-if="row.can_retry_failed" class="btn-ghost !text-xs" :disabled="busy || data.running" @click="resume(row, 'failed')">重试失败</button>
          <button class="btn-ghost !text-xs text-red-400" :disabled="busy || row.running" @click="deleting = row">删除记录</button>
        </div>
      </div>
      <p class="text-sm text-gray-400"><button class="hover:text-brand hover:underline" @click="showResults(row.job_id)">已处理 {{ row.done }} / {{ row.total }} 部</button> · <button class="text-emerald-400 hover:underline" @click="showResults(row.job_id, 'succeeded')">成功 {{ row.ok }}</button> · <button class="hover:underline" :class="row.failed ? 'text-red-400' : ''" @click="showResults(row.job_id, 'failed')">失败 {{ row.failed }}</button> · 未完成 {{ row.pending }}</p>
      <p v-if="row.error" class="break-all text-xs text-red-400">{{ row.error }}</p>
    </article>
    <div v-if="data.total > 20" class="flex items-center justify-end gap-3 text-xs text-gray-400"><button class="btn-ghost" :disabled="page <= 1 || loading" @click="page--; load()">上一页</button><span>{{ page }} / {{ Math.ceil(data.total / 20) }}</span><button class="btn-ghost" :disabled="page * 20 >= data.total || loading" @click="page++; load()">下一页</button></div>
    <div v-if="selected" id="scrape-history-results" class="card scroll-mt-4 space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-3"><h3 class="text-sm text-gray-200">逐项结果 · {{ selected.slice(0, 8) }}</h3><div class="flex items-center gap-3"><select v-model="status" class="input !w-auto" aria-label="结果筛选" @change="detailPage = 1; openDetail(selected)"><option value="">全部结果</option><option value="failed">仅失败</option><option value="succeeded">仅成功</option></select><button class="text-xs text-gray-400" @click="selected = ''; detail = null; revision++">收起明细</button></div></div>
      <p v-if="detailBusy" class="text-xs text-gray-500">正在读取明细…</p>
      <p v-if="detail && !detail.items.length" class="text-xs text-gray-500">暂无符合条件的结果，未处理影片可通过继续任务处理。</p>
      <div v-for="row in detail?.items || []" :key="row.code" class="space-y-1 border-t border-gray-800 pt-3 text-xs"><p :class="row.ok ? 'text-emerald-400' : 'text-red-400'">{{ row.code }} · {{ row.ok ? '成功' : '失败' }}</p><p v-if="row.error" class="break-all text-red-400">{{ row.error }}</p><p v-for="warning in row.warnings || []" :key="warning" class="text-amber-400">{{ warning }}</p><p v-for="path in row.links || []" :key="path" class="break-all text-gray-500">{{ path }}</p></div>
      <div v-if="detail?.total > 30" class="flex items-center justify-end gap-3 text-xs text-gray-400"><button class="btn-ghost" :disabled="detailPage <= 1 || detailBusy" @click="detailPage--; openDetail(selected)">上一页结果</button><span>{{ detailPage }} / {{ Math.ceil(detail.total / 30) }}</span><button class="btn-ghost" :disabled="detailPage * 30 >= detail.total || detailBusy" @click="detailPage++; openDetail(selected)">下一页结果</button></div>
    </div>
    <ModalDialog :open="!!deleting" title="删除刮削记录" @update:open="value => { if (!value && !busy) deleting = null }" :close-on-backdrop="!busy" :close-on-esc="!busy">
      <p class="text-sm text-gray-300">删除后，此任务不再出现在历史和恢复选择中。影片、下载源及隔离备份会保留。</p>
      <div class="mt-5 flex justify-end gap-3"><button class="btn-ghost" :disabled="busy" @click="deleting = null">取消</button><button class="btn-primary" :disabled="busy" @click="remove">{{ busy ? '处理中…' : '确认删除记录' }}</button></div>
    </ModalDialog>
  </PagePanel>
</template>
