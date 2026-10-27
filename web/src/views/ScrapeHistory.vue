<script setup>
import { useVisibleTimers } from '@/composables/useVisibleTimers'
const { setTimeout, clearTimeout, setInterval, clearInterval } = useVisibleTimers()
import PagePanel from '@/components/PagePanel.vue'
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import http from '@/api'
import ModalDialog from '@/components/ModalDialog.vue'
import { useToast } from '@/composables/useToast'

const toast = useToast()
const data = ref({ items: [], total: 0 }), page = ref(1), loading = ref(false), error = ref('')
const results = reactive({})
const busy = ref(false), deleting = ref(null)
let timer, disposed = false, revision = 0
const phases = { completed: '已完成', partial: '部分失败', interrupted: '已中断', failed: '任务失败', cancelled: '已停止', running: '刮削中', checking: '检查中', quarantine: '备份中', backup: '备份中', clearing: '清理中', rebuilding: '刮削中' }
const kinds = { rebuild: '全部重新刮削', manual: '手动批量刮削', automatic: '自动刮削' }
const date = value => value ? new Date(value * 1000).toLocaleString() : '—'

const signature = row => `${row.done}:${row.ok}:${row.phase}`
async function load(force = false) {
  clearTimeout(timer)
  const request = ++revision
  loading.value = true
  try {
    const result = await http.get('/scrape/history', { params: { page: page.value, size: 20 } })
    if (disposed || request !== revision) return
    const ids = new Set(result.items.map(row => row.job_id))
    for (const id of Object.keys(results)) if (!ids.has(id)) delete results[id]
    for (const row of result.items) {
      results[row.job_id] ||= { data: null, page: 1, status: '', busy: false, error: '', request: 0, signature: '' }
    }
    data.value = result
    error.value = ''
    // 本页自动显示结果，限制并发，已结束且未变化的任务不重复拉取。
    const pending = result.items.filter(row => force === true || results[row.job_id].signature !== signature(row))
    await Promise.all(Array.from({ length: Math.min(4, pending.length) }, async () => {
      while (pending.length && !disposed && request === revision) await loadResults(pending.shift())
    }))
  } catch (err) { if (!disposed && request === revision) error.value = err.message }
  finally {
    if (!disposed && request === revision) {
      loading.value = false
      timer = setTimeout(load, 5000)
    }
  }
}
async function loadResults(row) {
  const state = results[row.job_id]
  if (!state) return
  const request = ++state.request
  state.busy = true
  try {
    const result = await http.get(`/scrape/history/${row.job_id}`, { params: { page: state.page, size: 30, status: state.status } })
    if (disposed || results[row.job_id] !== state || request !== state.request) return
    state.data = result; state.error = ''; state.signature = signature(row)
  } catch (err) {
    if (!disposed && results[row.job_id] === state && request === state.request) state.error = err.message
  } finally {
    if (results[row.job_id] === state && request === state.request) state.busy = false
  }
}
function showResults(row, filter = '') {
  const state = results[row.job_id]
  state.page = 1; state.status = filter
  state.data = null
  loadResults(row)
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
    delete results[deleting.value.job_id]
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
  <PagePanel :mobile-summary="`${data.total} 个任务`" header-label="记录说明与刷新" content-class="space-y-4">
    <template #header>

    <div class="flex items-start justify-between gap-4">
      <div><h2 class="font-medium text-gray-200">刮削记录 <span class="ml-2 text-xs text-gray-500">{{ data.total }} 个任务</span></h2><p class="mt-1 text-xs text-gray-500">保存自动刮削、手动批量刮削与全部重建结果。继续任务保留已完成影片；删除记录保留影片、下载源和备份。</p></div>
      <button class="btn-ghost shrink-0" :disabled="loading || busy" @click="load(true)">刷新</button>
    </div>
    </template>

    <p v-if="error" class="text-sm text-red-400" role="alert">{{ error }}</p>
    <p v-if="!data.items.length" class="card text-sm text-gray-500">{{ loading ? '正在读取记录…' : '暂无刮削记录' }}</p>
    <article v-for="row in data.items" :key="row.job_id" class="card space-y-3">
      <div class="flex flex-wrap justify-between gap-3">
        <div><p class="text-sm font-medium text-gray-200">{{ kinds[row.kind] || row.kind }} <span class="ml-2 text-xs" :class="row.running ? 'text-brand' : row.failed ? 'text-amber-400' : 'text-gray-400'">{{ phases[row.phase] || row.phase }}</span></p><p class="mt-1 text-xs text-gray-500">{{ date(row.started_at) }} · {{ row.job_id.slice(0, 8) }} <span v-if="row.parent_id">· 续接 {{ row.parent_id.slice(0, 8) }}</span></p></div>
        <div class="flex flex-wrap items-center gap-2 text-xs">
          <button v-if="row.can_resume" class="btn-primary !text-xs" :disabled="busy || data.running" @click="resume(row, 'unfinished')">继续未完成</button>
          <button v-if="row.can_retry_failed" class="btn-ghost !text-xs" :disabled="busy || data.running" @click="resume(row, 'failed')">重试失败</button>
          <button class="btn-ghost !text-xs text-red-400" :disabled="busy || row.running" @click="deleting = row">删除记录</button>
        </div>
      </div>
      <p class="text-sm text-gray-400"><button class="hover:text-brand hover:underline" @click="showResults(row)">已处理 {{ row.done }} / {{ row.total }} 部</button> · <button class="text-emerald-400 hover:underline" @click="showResults(row, 'succeeded')">成功 {{ row.ok }}</button> · <button class="hover:underline" :class="row.failed ? 'text-red-400' : ''" @click="showResults(row, 'failed')">失败 {{ row.failed }}</button> · 未完成 {{ row.pending }}</p>
      <p v-if="row.error" class="break-all text-xs text-red-400">{{ row.error }}</p>
      <section v-if="results[row.job_id]" class="space-y-2 border-t border-gray-800 pt-2" :aria-label="`${row.job_id} 逐项结果`">
        <div v-if="row.done > 1 || results[row.job_id].status" class="flex items-center justify-between gap-2 text-xs text-gray-500">
          <span>逐项结果</span>
          <select v-model="results[row.job_id].status" class="input !w-auto" :aria-label="`${row.job_id} 结果筛选`" @change="showResults(row, results[row.job_id].status)"><option value="">全部结果</option><option value="failed">仅失败</option><option value="succeeded">仅成功</option></select>
        </div>
        <p v-if="results[row.job_id].busy" class="text-xs text-gray-500">正在读取结果…</p>
        <p v-if="results[row.job_id].error" class="text-xs text-red-400">{{ results[row.job_id].error }} <button class="text-brand" @click="loadResults(row)">重试读取</button></p>
        <p v-else-if="results[row.job_id].data && !results[row.job_id].data.items.length" class="text-xs text-gray-500">{{ row.running ? '任务执行中，结果会自动更新。' : '暂无符合条件的结果，未处理影片可通过继续任务处理。' }}</p>
        <div v-for="item in results[row.job_id].data?.items || []" :key="item.code" class="space-y-1 text-xs">
          <p :class="item.ok ? 'text-emerald-400' : 'text-red-400'">{{ item.code }} · {{ item.ok ? '成功' : '失败' }}</p>
          <p v-if="item.error" class="break-all text-red-400">{{ item.error }}</p>
          <p v-for="warning in item.warnings || []" :key="warning" class="break-all text-amber-400">{{ warning }}</p>
          <p v-for="path in item.links || []" :key="path" class="break-all text-gray-500">{{ path }}</p>
        </div>
        <div v-if="results[row.job_id].data?.total > 30" class="flex flex-wrap items-center justify-end gap-2 text-xs text-gray-400">
          <button class="btn-ghost" :disabled="results[row.job_id].page <= 1 || results[row.job_id].busy" @click="results[row.job_id].page--; loadResults(row)">上一页结果</button>
          <span>{{ results[row.job_id].page }} / {{ Math.ceil(results[row.job_id].data.total / 30) }}</span>
          <button class="btn-ghost" :disabled="results[row.job_id].page * 30 >= results[row.job_id].data.total || results[row.job_id].busy" @click="results[row.job_id].page++; loadResults(row)">下一页结果</button>
        </div>
      </section>
    </article>
    <div v-if="data.total > 20" class="flex items-center justify-end gap-3 text-xs text-gray-400"><button class="btn-ghost" :disabled="page <= 1 || loading" @click="page--; load()">上一页</button><span>{{ page }} / {{ Math.ceil(data.total / 20) }}</span><button class="btn-ghost" :disabled="page * 20 >= data.total || loading" @click="page++; load()">下一页</button></div>
    <ModalDialog :open="!!deleting" title="删除刮削记录" @update:open="value => { if (!value && !busy) deleting = null }" :close-on-backdrop="!busy" :close-on-esc="!busy">
      <p class="text-sm text-gray-300">删除后，此任务不再出现在历史和恢复选择中。影片、下载源及隔离备份会保留。</p>
      <div class="mt-5 flex justify-end gap-3"><button class="btn-ghost" :disabled="busy" @click="deleting = null">取消</button><button class="btn-primary" :disabled="busy" @click="remove">{{ busy ? '处理中…' : '确认删除记录' }}</button></div>
    </ModalDialog>
  </PagePanel>
</template>
