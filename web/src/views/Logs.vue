<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getLogFiles, getLogs } from '@/api'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/EmptyState.vue'

const toast = useToast()
const route = useRoute()
const lines = ref([])
const keyword = ref(String(route.query.keyword || ''))
const level = ref('')
const files = ref([])
const filename = ref('')
const autoScroll = ref(true)
const autoRefresh = ref(true)
const loading = ref(false)
const box = ref(null)
let timer = null, revision = 0, disposed = false, controller = null

const LEVELS = ['', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

const filtered = computed(() => {
  if (!level.value) return lines.value
  return lines.value.filter((line) => line.includes(`| ${level.value}`))
})

function lineClass(line) {
  if (line.includes('| ERROR')) return 'text-red-400'
  if (line.includes('| WARNING')) return 'text-amber-400'
  if (line.includes('| DEBUG')) return 'text-gray-600'
  return 'text-gray-400'
}

async function load() {
  clearTimeout(timer)
  controller?.abort()
  const pending = new AbortController()
  controller = pending
  const id = ++revision
  loading.value = true
  try {
    const data = await getLogs(500, keyword.value.trim(), filename.value, level.value, pending.signal)
    if (disposed || id !== revision) return
    lines.value = data.logs || []
    if (autoScroll.value) {
      await nextTick()
      if (box.value) box.value.scrollTop = box.value.scrollHeight
    }
  } catch (err) {
    if (!disposed && id === revision && !pending.signal.aborted) toast.error(err.message)
  } finally {
    if (!disposed && id === revision) {
      loading.value = false
      if (autoRefresh.value && !filename.value) timer = setTimeout(load, 5000)
    }
  }
}

async function refresh() {
  try {
    const data = await getLogFiles()
    if (!disposed) files.value = data.files || []
  } catch (err) {
    if (!disposed) toast.error(err.message)
  }
  if (!disposed) await load()
}

watch([filename, level], () => { lines.value = []; load() })
watch(autoRefresh, load)
onMounted(refresh)
onUnmounted(() => { disposed = true; revision++; controller?.abort(); clearTimeout(timer) })
</script>

<template>
  <div class="flex h-full min-h-0 min-w-0 flex-col gap-3 overflow-hidden">
    <div class="flex max-h-[45%] shrink-0 flex-wrap items-center gap-2 overflow-auto">
      <select v-model="filename" class="input w-full sm:w-60" aria-label="日志文件">
        <option value="">当前日志</option>
        <option v-for="file in files.filter(item => !item.current)" :key="file.name" :value="file.name">
          {{ file.name.slice(9, 19) }} · {{ Math.ceil(file.size / 1024) }} KB
        </option>
      </select>
      <input
        v-model="keyword"
        class="input w-full sm:w-64"
        placeholder="按关键词过滤"
        @keyup.enter="load"
      />
      <select v-model="level" class="input w-full sm:w-32">
        <option v-for="item in LEVELS" :key="item" :value="item">
          {{ item || '全部级别' }}
        </option>
      </select>
      <button class="btn-ghost px-3 py-1.5 text-xs" :disabled="loading" @click="refresh">
        刷新
      </button>
      <label class="flex items-center gap-1.5 text-xs text-gray-400">
        <input v-model="autoRefresh" type="checkbox" :disabled="!!filename" class="accent-emerald-500" />
        自动刷新
      </label>
      <span v-if="filename" class="text-xs text-gray-500">历史日志 · 自动刷新已暂停</span>
      <label class="flex items-center gap-1.5 text-xs text-gray-400">
        <input v-model="autoScroll" type="checkbox" class="accent-emerald-500" />
        滚动到底部
      </label>
    </div>

    <div
      v-if="filtered.length"
      ref="box"
      class="min-h-0 flex-1 overflow-auto overscroll-contain rounded-xl border border-gray-800 bg-black/40 p-3"
      tabindex="0"
      aria-label="日志内容"
    >
      <pre
        v-for="(line, index) in filtered"
        :key="index"
        class="whitespace-pre-wrap break-all font-mono text-[11px] leading-5"
        :class="lineClass(line)"
        >{{ line }}</pre
      >
    </div>

    <EmptyState v-else text="没有日志" hint="日志文件位于数据目录的 logs 子目录" />
  </div>
</template>
