<script setup>
import PagePanel from '@/components/PagePanel.vue'
import { onMounted, ref } from 'vue'
import { getDashboard, getReleaseToday, downloadAll } from '@/api'
import { useToast } from '@/composables/useToast'
import { useCodeSelection } from '@/composables/useCodeSelection'
import CodeCard from '@/components/CodeCard.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import SelectionBar from '@/components/SelectionBar.vue'
import AppIcon from '@/components/AppIcon.vue'

const toast = useToast()
const stats = ref(null)
const today = ref([])
const loading = ref(true)
const running = ref(false)
const todayLabel = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: 'long', day: 'numeric', weekday: 'long' }).format(new Date())
const shortcuts = [
  { name: 'Library', title: '浏览媒体库', description: '收藏、片单与观看记录', icon: 'library' },
  { name: 'Downloads', title: '查看下载', description: '下载进度与长期做种', icon: 'download' },
  { name: 'Task', title: '管理任务', description: '抓取、同步与自动整理', icon: 'task' },
]

// label 按 code.status 的真实口径来 —— status 是单值流转字段，
// 「待搜种」只含还没推给下载器的，不是订阅过的总数；actors 统计的是
// 演员订阅表，不是番号里出现过的演员
const TILES = [
  { to: { name: 'Subscribe', query: { status: 'all' } }, key: 'total', label: '番号总数', color: 'text-gray-200' },
  { to: { name: 'Subscribe', query: { status: '1' } }, key: 'subscribed', label: '待搜种', color: 'text-blue-400' },
  { to: { name: 'Downloads', query: { code_status: '2' } }, key: 'downloading', label: '下载中', color: 'text-amber-400' },
  { to: { name: 'Downloads', query: { tab: 'history', code_status: '3' } }, key: 'downloaded', label: '已下载', color: 'text-emerald-400' },
  { to: { name: 'Subscribe', query: { status: '4' } }, key: 'completed', label: '已入库', color: 'text-emerald-300' },
  { to: { name: 'Actors', query: { mode: 'subscribed' } }, key: 'actors', label: '已订阅演员', color: 'text-gray-200' },
]

async function load() {
  loading.value = true
  try {
    const [statsData, todayData] = await Promise.all([
      getDashboard(),
      getReleaseToday().catch(() => ({ items: [] })),
    ])
    stats.value = statsData
    today.value = todayData?.items || []
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

async function triggerDownload() {
  running.value = true
  try {
    await downloadAll()
    toast.success('订阅下载任务已触发')
  } catch (err) {
    toast.error(err.message)
  } finally {
    running.value = false
  }
}

const sel = useCodeSelection(today, load)

onMounted(load)
</script>

<template>
  <PagePanel :collapse-header="false" content-class="space-y-5" class="overview-page">
    <template #header>
      <div class="overview-intro !mb-0">
        <div><div class="section-eyebrow">OVERVIEW <span class="mx-1 text-gray-700">/</span> {{ todayLabel }}</div><h2>你的收藏，井然有序。</h2><p>从发现到入库，在这里管理每一部值得留下的影片。</p></div>
        <div class="flex shrink-0 items-center gap-2"><button class="icon-button border border-gray-800" aria-label="刷新总览" :disabled="loading" @click="load"><AppIcon name="refresh" :size="17" :class="{'animate-spin':loading}" /></button><button class="btn-primary !text-xs" :disabled="running" @click="triggerDownload"><AppIcon name="play" :size="14" />{{ running ? '提交中…' : '执行订阅下载' }}</button></div>
      </div>
    </template>
    <div class="overview-stat-grid" aria-label="收藏与订阅统计">
      <RouterLink v-for="tile in TILES" :key="tile.key" :to="tile.to" class="overview-stat" :aria-label="`查看${tile.label} ${stats?.[tile.key] ?? ''}`"><div class="flex items-center justify-between gap-1"><span class="text-xs text-gray-400">{{ tile.label }}</span><AppIcon name="chevron" :size="11" class="text-gray-600" /></div><p class="overview-stat-value" :class="tile.color">{{ stats?.[tile.key] ?? '—' }}</p></RouterLink>
    </div>
    <div class="overview-shortcuts" aria-label="继续工作">
      <RouterLink v-for="item in shortcuts" :key="item.name" :to="{name:item.name}" class="workflow-link"><span class="workflow-icon"><AppIcon :name="item.icon" :size="18" /></span><div class="min-w-0 flex-1"><p class="text-xs font-medium text-gray-200 sm:text-sm">{{ item.title }}</p><p class="workflow-description mt-1 text-[11px] text-gray-500">{{ item.description }}</p></div><AppIcon name="arrow" :size="16" class="text-gray-600" /></RouterLink>
    </div>
    <section>
      <div class="section-heading"><div class="flex flex-wrap items-center gap-3"><h2>今日上新</h2><span class="badge border border-gray-800 text-gray-400">{{ today.length }} 部</span><SelectionBar v-if="today.length" :sel="sel" /></div><RouterLink :to="{name:'Rank'}" class="flex items-center gap-1 text-xs text-gray-400 hover:text-brand">浏览榜单<AppIcon name="arrow" :size="14" /></RouterLink></div>
      <LoadingBlock v-if="loading" :rows="2" />
      <div v-else-if="today.length" class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3"><CodeCard v-for="item in today" :key="item.code" :item="item" :selectable="sel.active.value" :selected="sel.isSelected(item.code)" @toggle-select="sel.toggle" @changed="load" /></div>
      <EmptyState v-else text="今天还没有新片" hint="抓取完成后，新发现的影片会出现在这里。"><RouterLink :to="{name:'Task'}" class="btn-ghost mt-2 !text-xs">查看抓取任务<AppIcon name="arrow" :size="13" /></RouterLink></EmptyState>
    </section>
  </PagePanel>
</template>
