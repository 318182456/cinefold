<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { getRank, getRankSubscription, refreshRank, subscribeRank, subscribeCode } from '@/api'
import { useToast } from '@/composables/useToast'
import { useCodeSelection } from '@/composables/useCodeSelection'
import CodeCard from '@/components/CodeCard.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import SelectionBar from '@/components/SelectionBar.vue'

const toast = useToast()
const items = ref([])
const loading = ref(false)
const route = useRoute(), router = useRouter()
const rankType = computed({ get: () => ['daily','weekly','monthly'].includes(route.query.rank_type) ? route.query.rank_type : 'daily', set: value => router.replace({ query: { ...route.query, rank_type: value === 'daily' ? undefined : value } }) })
const page = ref(1)
const size = 12
const subscription = ref({running:false,phase:'idle'}), subscribing = ref(false), subscriptionError = ref('')
const rankInfo = ref({refreshing:false,phase:'idle'}), rankError = ref(''), refreshingRank = ref(false)
let rankTimer
let timer, disposed = false, request = 0, pollRequest = 0

const TYPES = [
  { value: 'daily', label: '日榜' },
  { value: 'weekly', label: '周榜' },
  { value: 'monthly', label: '月榜' },
]

// 榜单是一次抓取整批后缓存的，接口没有 offset/total，所以在前端切页
const pages = computed(() => Math.max(Math.ceil(items.value.length / size), 1))
const paged = computed(() => items.value.slice((page.value - 1) * size, page.value * size))

async function load(silent = false) {
  clearTimeout(rankTimer)
  const id = ++request, kind = rankType.value
  if (!silent) loading.value = true
  try {
    const data = await getRank(kind)
    if (disposed || id !== request || rankType.value !== kind) return
    items.value = data.items || []
    rankInfo.value = data; rankError.value = ''
    if (data.refreshing) rankTimer = setTimeout(() => load(true),1800)
    // 订阅后重新加载，条数变少时当前页可能已越界
    if (page.value > pages.value) page.value = pages.value
  } catch (err) {
    if (!disposed && id === request) { rankError.value = err.message; if (rankInfo.value.refreshing) rankTimer=setTimeout(() => load(true),5000) }
  } finally {
    if (id === request) loading.value = false
  }
}

async function refreshList() {
  if (refreshingRank.value || rankInfo.value.refreshing) return
  refreshingRank.value = true
  const kind = rankType.value, id = ++request
  clearTimeout(rankTimer)
  try {
    const data = await refreshRank(kind)
    if (disposed || kind !== rankType.value || id !== request) return
    rankInfo.value = data; items.value = data.items || []; rankError.value = ''
    page.value = 1; sel.clear()
    if (data.refreshing) rankTimer = setTimeout(() => load(true),1800)
  } catch (err) { if (!disposed && id === request) rankError.value=err.message }
  finally { refreshingRank.value = false }
}
async function pollSubscription() {
  clearTimeout(timer)
  if (disposed) return
  const id = ++pollRequest, kind = rankType.value
  try {
    const state = await getRankSubscription(kind)
    if (disposed || id !== pollRequest || kind !== rankType.value) return
    const changed = state.job_id !== subscription.value.job_id || state.done !== subscription.value.done || state.phase !== subscription.value.phase
    subscription.value = state; subscriptionError.value = ''
    if (changed && state.phase !== 'idle') await load(true)
    if (!disposed && id === pollRequest && kind === rankType.value && state.running) timer = setTimeout(pollSubscription, 1800)
  } catch (err) {
    if (!disposed && id === pollRequest) { subscriptionError.value = err.message; timer=setTimeout(pollSubscription,5000) }
  }
}
async function subscribeAll() {
  if (subscribing.value || subscription.value.running) return
  subscribing.value = true
  const kind = rankType.value
  try {
    const state = await subscribeRank(kind)
    if (kind === rankType.value) subscription.value = state
    toast.success('当前榜单订阅已在后台启动')
    pollSubscription()
  } catch (err) { toast.error(err.message) }
  finally { subscribing.value = false }
}

async function subOne(code) {
  try {
    await subscribeCode(code)
    toast.success(`已订阅 ${code}`)
    load()
  } catch (err) {
    toast.error(err.message)
  }
}

const sel = useCodeSelection(paged, load)

watch(rankType, () => {
  page.value = 1
  sel.clear()
  clearTimeout(timer); clearTimeout(rankTimer); pollRequest++
  rankInfo.value = {refreshing:false,phase:'idle'}; rankError.value=''; items.value=[]
  subscription.value = {running:false,phase:'idle'}; subscriptionError.value = ''
  load(); pollSubscription()
}, { immediate:true })
onUnmounted(() => { disposed=true; request++; pollRequest++; clearTimeout(timer); clearTimeout(rankTimer) })
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center gap-2">
      <button
        v-for="type in TYPES"
        :key="type.value"
        class="btn px-3 py-1.5 text-xs"
        :class="rankType === type.value ? 'bg-brand text-white' : 'btn-ghost'"
        @click="rankType = type.value"
      >
        {{ type.label }}
      </button>
      <button class="btn-ghost ml-auto px-3 py-1.5 text-xs" :disabled="loading || refreshingRank || rankInfo.refreshing" @click="refreshList">{{ refreshingRank || rankInfo.refreshing ? '正在刷新榜单…' : '刷新榜单' }}</button>
      <button class="btn-ghost px-3 py-1.5 text-xs" :disabled="loading || !items.length || subscribing || subscription.running || !!subscriptionError" @click="subscribeAll">
        {{ subscribing || subscription.running ? '正在订阅…' : '一键订阅当前榜单' }}
      </button>
    </div>

    <div v-if="rankInfo.refreshing || rankInfo.fetched_at || rankInfo.error || rankError" class="rounded-lg border border-gray-800 p-3 text-xs text-gray-400 space-y-2"><p v-if="rankInfo.refreshing" class="text-brand">正在后台读取{{ TYPES.find(item => item.value === rankType)?.label }}，可以离开此页。{{ items.length ? '先显示已有榜单，获取成功后自动更新。' : '正在获取排名，缺少的影片资料会随后补齐。' }}</p><p v-if="rankInfo.fetched_at">榜单获取时间：{{ new Date(rankInfo.fetched_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}) }}<span v-if="rankInfo.stale" class="text-amber-400"> · 旧快照</span></p><p v-if="rankInfo.error || rankError" class="text-amber-400">{{ rankError || rankInfo.error }}{{ items.length ? '，当前保留上次榜单。' : '' }}</p></div>
    <div v-if="subscription.phase !== 'idle' || subscriptionError" class="card space-y-2 text-xs text-gray-400"><p>{{ ({queued:'排队中',running:'正在订阅',completed:'订阅处理完成',partial:'部分订阅失败',interrupted:'任务已中断',failed:'任务失败'})[subscription.phase] || subscription.phase }}<span v-if="subscription.total != null"> · {{ subscription.done || 0 }} / {{ subscription.total }} 部 · 新增 {{ subscription.subscribed || 0 }} · 已订阅/下载中/已入库 {{ subscription.existing || 0 }} · 被过滤 {{ subscription.filtered || 0 }}</span></p><p v-if="subscription.current" class="text-brand">{{ subscription.current }}</p><p v-if="subscription.error || subscriptionError" class="text-red-400">{{ subscription.error || subscriptionError }}</p><p v-for="row in subscription.failures || []" :key="row.code" class="text-red-400">{{ row.code }} · {{ row.error }}</p><p v-if="subscription.running">任务在后台执行，完成后卡片会自动更新；也可以离开此页。</p></div>
    <SelectionBar v-if="items.length" :sel="sel" />

    <LoadingBlock v-if="loading" :rows="4" />

    <template v-else-if="items.length">
      <div class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        <template v-for="item in paged" :key="item.code">
          <CodeCard
            v-if="item.title || item.status !== undefined"
            :item="item"
            :selectable="sel.active.value"
            :selected="sel.isSelected(item.code)"
            @toggle-select="sel.toggle"
            @changed="load"
          />
          <div
            v-else
            class="card flex items-center justify-between"
            :class="[
              sel.active.value ? 'cursor-pointer' : '',
              sel.active.value && sel.isSelected(item.code) ? 'border-brand bg-brand/5' : '',
            ]"
            @click="sel.active.value && sel.toggle(item.code)"
          >
            <span class="flex items-center gap-2">
              <input
                v-if="sel.active.value"
                type="checkbox"
                class="h-3.5 w-3.5 cursor-pointer accent-brand"
                :checked="sel.isSelected(item.code)"
                @click.stop="sel.toggle(item.code)"
              />
              <span class="font-mono text-sm text-brand">{{ item.code }}</span>
            </span>
            <button class="btn-primary px-2.5 py-1 text-xs" @click.stop="subOne(item.code)">
              订阅
            </button>
          </div>
        </template>
      </div>

      <div v-if="pages > 1" class="flex items-center justify-center gap-3 pt-2">
        <button class="btn-ghost px-3 py-1.5 text-xs" :disabled="page <= 1" @click="page--">
          上一页
        </button>
        <span class="text-xs tabular-nums text-gray-500">{{ page }} / {{ pages }}</span>
        <button class="btn-ghost px-3 py-1.5 text-xs" :disabled="page >= pages" @click="page++">
          下一页
        </button>
      </div>
    </template>

    <EmptyState
      v-else
      :text="rankInfo.refreshing ? '正在获取榜单' : rankError || rankInfo.error ? '榜单暂时无法更新' : '暂无榜单'"
      :hint="rankInfo.refreshing ? '后台完成后自动显示，无需反复刷新' : '源站不可达时保留旧榜单；可点击刷新榜单重试，并检查代理与过盾服务'"
    />
  </div>
</template>
