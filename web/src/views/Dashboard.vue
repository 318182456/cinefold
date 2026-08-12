<script setup>
import { onMounted, ref } from 'vue'
import { getDashboard, getReleaseToday, downloadAll } from '@/api'
import { useToast } from '@/composables/useToast'
import { useCodeSelection } from '@/composables/useCodeSelection'
import CodeCard from '@/components/CodeCard.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import SelectionBar from '@/components/SelectionBar.vue'

const toast = useToast()
const stats = ref(null)
const today = ref([])
const loading = ref(true)
const running = ref(false)

const TILES = [
  { key: 'total', label: '番号总数', color: 'text-gray-200' },
  { key: 'subscribed', label: '已订阅', color: 'text-blue-400' },
  { key: 'downloading', label: '下载中', color: 'text-amber-400' },
  { key: 'downloaded', label: '已下载', color: 'text-emerald-400' },
  { key: 'completed', label: '已入库', color: 'text-emerald-300' },
  { key: 'actors', label: '演员', color: 'text-gray-200' },
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
  <div class="space-y-6">
    <!-- 统计 -->
    <div class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <div v-for="tile in TILES" :key="tile.key" class="card">
        <p class="text-xs text-gray-500">{{ tile.label }}</p>
        <p class="mt-1 text-2xl font-semibold tabular-nums" :class="tile.color">
          {{ stats?.[tile.key] ?? '—' }}
        </p>
      </div>
    </div>

    <div class="flex gap-2">
      <button class="btn-primary" :disabled="running" @click="triggerDownload">
        立即执行订阅下载
      </button>
      <button class="btn-ghost" @click="load">刷新</button>
    </div>

    <!-- 今日上新 -->
    <section>
      <div class="mb-3 flex flex-wrap items-center gap-3">
        <h2 class="text-sm font-medium text-gray-300">今日上新</h2>
        <SelectionBar v-if="today.length" :sel="sel" />
      </div>
      <LoadingBlock v-if="loading" :rows="2" />
      <div v-else-if="today.length" class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <CodeCard
          v-for="item in today"
          :key="item.code"
          :item="item"
          :selectable="sel.active.value"
          :selected="sel.isSelected(item.code)"
          @toggle-select="sel.toggle"
          @changed="load"
        />
      </div>
      <EmptyState v-else text="今天还没有新片" hint="数据来自定时抓取任务" />
    </section>
  </div>
</template>
