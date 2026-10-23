<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import http, { subscribeCode } from '@/api'
import PagePanel from '@/components/PagePanel.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import EmptyState from '@/components/EmptyState.vue'
import { useToast } from '@/composables/useToast'

const toast = useToast()
const data = ref({ items: [], total: 0 }), keyword = ref(''), status = ref(''), page = ref(1)
const loading = ref(false), error = ref(''), selected = ref(null), detail = ref(null), detailError = ref(''), subscribing = ref('')
const phases = { completed: '已完成', partial: '部分失败', failed: '执行失败', running: '执行中', interrupted: '中断待核对' }
const sources = { emby: 'Emby 删除回调', cleanup: '系统 / 手动联动清理', manual: '手动清除记录', prune: '失效记录清理' }
const date = value => value ? new Date(value).toLocaleString('zh-CN') : '—'
let request = 0, detailRequest = 0, disposed = false, timer
async function load() {
  clearTimeout(timer)
  const id = ++request
  loading.value = true
  try {
    const result = await http.get('/medialinks/deletion-history', { params: { keyword: keyword.value.trim(), status: status.value, page: page.value, size: 20 } })
    if (disposed || id !== request) return
    data.value = result; error.value = ''
  } catch (err) { if (!disposed && id === request) error.value = err.message }
  finally {
    if (!disposed && id === request) {
      loading.value = false
      if (data.value.items.some(row => row.status === 'running')) timer = setTimeout(load, 3000)
    }
  }
}
function search() { page.value = 1; load() }
function turn(delta) { page.value += delta; load() }
async function show(row) {
  selected.value = row; detail.value = null; detailError.value = ''
  const id = ++detailRequest
  try {
    const result = await http.get(`/medialinks/deletion-history/${row.id}`)
    if (!disposed && id === detailRequest && selected.value?.id === row.id) detail.value = result
  } catch (err) { if (!disposed && id === detailRequest) detailError.value = err.message }
}
async function resubscribe(row) {
  if (subscribing.value) return
  subscribing.value = row.id
  try { await subscribeCode(row.code); toast.success(`${row.code} 已重新订阅，等待订阅下载任务检索；删除历史继续保留`) }
  catch (err) { toast.error(err.message) }
  finally { subscribing.value = '' }
}
onMounted(load)
onUnmounted(() => { disposed = true; request++; detailRequest++; clearTimeout(timer) })
</script>

<template>
  <PagePanel :mobile-summary="`${data.total} 条记录${keyword ? ' · ' + keyword : ''}`" header-label="历史筛选">
    <template #header>
      <form class="flex flex-wrap gap-2" @submit.prevent="search">
        <input v-model="keyword" class="input min-w-40 flex-1" placeholder="搜索番号、标题或原路径" aria-label="搜索删除历史">
        <select v-model="status" class="input !w-auto" aria-label="删除结果" @change="search"><option value="">全部结果</option><option v-for="(label, key) in phases" :key="key" :value="key">{{ label }}</option></select>
        <button class="btn-ghost" :disabled="loading">查询 / 刷新</button>
      </form>
    </template>
    <p class="text-xs text-gray-500">保留删除前的影片、路径和下载信息；重新下载后仍可追溯。历史从此功能启用后开始记录，旧日志不会自动转换为删除记录。</p>
    <p v-if="error" role="alert" class="text-sm text-red-400">读取失败：{{ error }}。<button class="text-brand" @click="load">重试</button></p>
    <p v-if="loading && !data.items.length" class="py-8 text-center text-gray-500">读取中…</p>
    <article v-for="row in data.items" :key="row.id" class="card space-y-2">
      <div class="flex items-center justify-between gap-2"><span class="font-mono text-sm text-brand">{{ row.code || '未识别番号' }}</span><span class="badge" :class="['partial','failed','interrupted'].includes(row.status) ? 'bg-amber-500/10 text-amber-400' : 'bg-gray-800 text-gray-400'">{{ phases[row.status] || row.status }}</span></div>
      <p v-if="row.title" class="line-clamp-2 text-sm text-gray-300">{{ row.title }}</p>
      <p class="text-xs text-gray-500">{{ date(row.created_at) }} · {{ sources[row.source] || row.source }} · {{ row.action === 'record_remove' ? '仅清关联记录，未删文件' : '联动清理' }}</p>
      <p class="truncate text-xs text-gray-500" :title="row.link_path">{{ row.link_path }}</p>
      <div class="flex flex-wrap gap-2"><button class="btn-ghost !text-xs" @click="show(row)">查看删除明细</button><button v-if="row.code && row.status !== 'running'" class="btn-ghost !text-xs" :disabled="!!subscribing" @click="resubscribe(row)">{{ subscribing === row.id ? '订阅中…' : '重新订阅' }}</button><RouterLink v-if="row.code" :to="{ name:'Search',query:{keyword:row.code} }" class="btn-ghost !text-xs">查看影片</RouterLink></div>
    </article>
    <EmptyState v-if="!loading && !error && !data.items.length" text="暂无删除历史" hint="真实联动清理或清除关联记录后会保留记录；演练不会记成真实删除。" />
    <div v-if="data.total > 20" class="flex items-center justify-center gap-3 text-xs"><button class="btn-ghost" :disabled="page <= 1 || loading" @click="turn(-1)">上一页</button><span>{{ page }} / {{ Math.ceil(data.total / 20) }}</span><button class="btn-ghost" :disabled="page * 20 >= data.total || loading" @click="turn(1)">下一页</button></div>
    <ModalDialog :open="!!selected" :title="`${selected?.code || ''} 删除明细`" size="xl" scrollable @update:open="value => { if (!value) { selected = null; detailRequest++ } }">
      <p v-if="detailError" class="text-sm text-red-400">{{ detailError }}</p><p v-else-if="!detail" class="text-sm text-gray-500">读取明细中…</p>
      <div v-else class="space-y-4 text-xs text-gray-400">
        <p>{{ sources[detail.source] || detail.source }} · {{ phases[detail.status] }} · {{ date(detail.created_at) }} → {{ date(detail.finished_at) }}</p>
        <p v-if="detail.action === 'record_remove'">仅清除关联记录，未删除视频或下载器任务。</p>
        <p v-if="detail.status === 'interrupted'" class="text-amber-400">服务中断，部分操作可能已执行。请核对以下路径和下载器；不会自动重新删除。</p>
        <section><h3 class="mb-2 text-gray-200">删除前的关联</h3><div v-for="(row, i) in detail.snapshot.links || []" :key="i" class="mb-2 space-y-1 break-all"><p>{{ row.code }}</p><p>源文件：{{ row.source_path || '未登记' }}</p><p>播放文件：{{ row.link_path }}</p></div></section>
        <section><h3 class="mb-2 text-gray-200">原下载信息</h3><div v-for="row in detail.snapshot.downloads || []" :key="row.hash" class="mb-2 space-y-1 break-all"><p>{{ row.code }} · {{ row.site || '来源未记录' }} · {{ row.create_time }}</p><p>Hash：{{ row.hash }}</p><p>{{ row.save_path }}</p></div><p v-if="!detail.snapshot.downloads?.length">没有对应的下载记录</p></section>
        <section v-for="[key,label] in [['torrents_deleted','已删除种子'],['torrents_kept','保留做种'],['files_deleted','已删除文件'],['files_kept','保留文件'],['links_deleted','处理的播放链接'],['sidecars_deleted','已删除附属文件'],['dirs_deleted','已删除目录'],['errors','错误与未完成事项']]" :key="key"><template v-if="detail.result[key]?.length"><h3 class="mb-1 text-gray-200">{{ label }} · {{ detail.result[key].length }}</h3><p v-for="(item,i) in detail.result[key]" :key="i" class="break-all">{{ item }}</p></template></section>
        <p v-if="detail.error" class="text-red-400">{{ detail.error }}</p>
      </div>
    </ModalDialog>
  </PagePanel>
</template>
