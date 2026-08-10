<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { getLogs } from '@/api'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/EmptyState.vue'

const toast = useToast()
const lines = ref([])
const keyword = ref('')
const level = ref('')
const autoScroll = ref(true)
const autoRefresh = ref(false)
const loading = ref(false)
const box = ref(null)
let timer = null

const LEVELS = ['', 'DEBUG', 'INFO', 'WARNING', 'ERROR']

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
  loading.value = true
  try {
    const data = await getLogs(500, keyword.value.trim())
    lines.value = data.logs || []
    if (autoScroll.value) {
      await nextTick()
      if (box.value) box.value.scrollTop = box.value.scrollHeight
    }
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

watch(autoRefresh, (on) => {
  clearInterval(timer)
  if (on) timer = setInterval(load, 5000)
})

onMounted(load)
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap items-center gap-2">
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
      <button class="btn-ghost px-3 py-1.5 text-xs" :disabled="loading" @click="load">
        刷新
      </button>
      <label class="flex items-center gap-1.5 text-xs text-gray-400">
        <input v-model="autoRefresh" type="checkbox" class="accent-emerald-500" />
        自动刷新
      </label>
      <label class="flex items-center gap-1.5 text-xs text-gray-400">
        <input v-model="autoScroll" type="checkbox" class="accent-emerald-500" />
        滚动到底部
      </label>
    </div>

    <div
      v-if="filtered.length"
      ref="box"
      class="h-[calc(100vh-16rem)] overflow-auto rounded-xl border border-gray-800 bg-black/40 p-3"
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
