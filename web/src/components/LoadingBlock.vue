<script setup>
import { onMounted, ref } from 'vue'
import { useVisibleTimers } from '@/composables/useVisibleTimers'

defineProps({
  rows: { type: Number, default: 3 },
  label: { type: String, default: '正在加载，请稍候…' },
  slowHint: { type: String, default: '响应较慢，仍在等待结果。你可以稍后重试。' },
})
const elapsed = ref(0)
const started = Date.now()
const { setInterval } = useVisibleTimers()
onMounted(() => setInterval(() => { elapsed.value = Math.floor((Date.now() - started) / 1000) }, 1000))
</script>

<template>
  <div class="loading-block" :class="{'loading-block-compact': rows === 0}" role="status" aria-live="polite" aria-busy="true">
    <div class="loading-message">
      <svg class="loading-spinner" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5" opacity=".2" /><path d="M12 3a9 9 0 0 1 9 9" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" /></svg>
      <span>{{ label }}</span><span v-if="elapsed >= 3" class="loading-elapsed" aria-hidden="true">已等待 {{ elapsed }} 秒</span>
    </div>
    <p v-if="elapsed >= 12" class="loading-hint">{{ slowHint }}</p>
    <div v-if="rows > 0" class="loading-placeholders" aria-hidden="true"><div v-for="i in rows" :key="i" class="loading-placeholder" /></div>
  </div>
</template>
