<script setup>
import { computed } from 'vue'
const props = defineProps({ state: { type: Object, required: true }, phase: { type: String, default: '' } })
const progress = computed(() => props.state.progress)
const steps = computed(() => Object.values(props.state.progress_steps || {}))
const percent = computed(() => progress.value?.total > 0 ? Math.min(100, progress.value.done / progress.value.total * 100) : null)
const bytes = value => value >= 1073741824 ? `${(value / 1073741824).toFixed(2)} GB` : value >= 1048576 ? `${(value / 1048576).toFixed(1)} MB` : `${Math.max(0, value || 0)} B`
function duration(value) {
  const seconds = Math.max(0, Math.floor(value || 0))
  return seconds >= 3600 ? `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分` : seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`
}
</script>

<template>
  <div class="space-y-2 text-xs" aria-live="polite">
    <template v-if="progress">
      <div class="flex flex-wrap justify-between gap-2 text-gray-300">
        <span>{{ progress.label }}<span v-if="progress.total"> · {{ progress.done || 0 }} / {{ progress.total }} {{ progress.unit || '项' }}</span><span v-else> · 正在处理</span></span>
        <span v-if="percent !== null">{{ percent.toFixed(1) }}%</span>
      </div>
      <div v-if="percent !== null" class="h-1.5 overflow-hidden rounded-full bg-gray-800" role="progressbar" :aria-label="progress.label" :aria-valuenow="progress.done || 0" :aria-valuemin="0" :aria-valuemax="progress.total"><div class="h-full rounded-full bg-brand transition-all" :style="{ width: `${percent}%` }"></div></div>
      <div v-else-if="state.running" class="h-1.5 overflow-hidden rounded-full bg-gray-800" role="progressbar" :aria-label="progress.label"><div class="h-full w-1/3 animate-pulse rounded-full bg-brand"></div></div>
      <p v-if="progress.bytes_total" class="text-gray-400">已备份 {{ bytes(progress.bytes_done) }} / {{ bytes(progress.bytes_total) }}</p>
      <p v-if="progress.current" class="break-all font-mono text-gray-400">当前：{{ progress.current }}</p>
      <p v-if="progress.current_total_bytes" class="text-gray-500">当前文件读写 {{ bytes(progress.current_bytes) }} / {{ bytes(progress.current_total_bytes) }}</p>
      <p class="text-gray-500">本阶段 {{ duration(progress.elapsed_seconds) }} · 总耗时 {{ duration(state.elapsed_seconds) }}<span v-if="state.running"> · {{ duration(progress.idle_seconds) }}前有进度更新</span></p>
      <p v-if="state.running && progress.idle_seconds >= 60" class="text-amber-300">当前操作已 {{ duration(progress.idle_seconds) }}没有新进度，可能正在等待磁盘、网络或文件同步；尚不能据此确认任务卡死。</p>
      <details v-if="steps.length" class="rounded-lg bg-gray-800/30 p-2"><summary class="cursor-pointer text-gray-400">阶段记录 · {{ steps.length }} 项（点击查看）</summary><div v-for="step in steps" :key="step.stage" class="mt-2 flex flex-wrap justify-between gap-2 text-gray-400"><span>{{ step.label }}</span><span>{{ step.done || 0 }}<span v-if="step.total"> / {{ step.total }}</span> {{ step.unit }} · {{ step.finished_at ? '已结束' : state.running ? '处理中' : state.phase === 'failed' ? '失败' : '已结束' }}</span></div></details>
    </template>
    <p v-else class="text-gray-400">{{ phase }}<span v-if="state.phase === 'rebuilding'"> · {{ state.done || 0 }} / {{ state.total || 0 }} 部</span><span v-else-if="state.running"> · 后台处理中，当前任务尚未提供分阶段数量。</span></p>
    <p v-if="state.phase === 'rebuilding' || state.done" class="text-gray-400">刮削完成 {{ state.done || 0 }} 部 · 成功 {{ state.ok || 0 }} 部 · 失败 {{ (state.done || 0) - (state.ok || 0) }} 部</p>
    <details v-if="state.cleanup_note_count" class="text-amber-300"><summary class="cursor-pointer">清理处理记录 {{ state.cleanup_note_count }} 项 · 原位保留 {{ state.retained_file_count || 0 }} 个文件 · 点击查看明细</summary><p v-for="(note, index) in state.cleanup_notes || []" :key="index" class="mt-2 break-all">{{ note.path }} {{ note.reason }}</p></details>
  </div>
</template>
