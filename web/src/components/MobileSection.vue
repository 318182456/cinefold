<script setup>
import { ref, useId } from 'vue'

defineProps({
  title: { type: String, default: '筛选与统计' },
  summary: { type: String, default: '' },
  collapsible: { type: Boolean, default: true },
})
const expanded = ref(false)
const id = `mobile-section-${useId()}`
</script>

<template>
  <div class="mobile-section min-w-0" :class="{ 'mobile-section-expanded': expanded, 'mobile-section-fixed': !collapsible }">
    <button v-if="collapsible" type="button" class="mobile-section-toggle flex min-h-9 w-full shrink-0 items-center gap-2 rounded-lg border border-gray-800 bg-gray-900/60 px-2.5 text-left text-xs sm:hidden" :aria-expanded="expanded" :aria-controls="id" @click="expanded = !expanded">
      <span class="shrink-0 text-gray-300">{{ title }}</span>
      <span v-if="summary" class="min-w-0 flex-1 truncate text-xs text-gray-500">{{ summary }}</span>
      <span class="ml-auto flex shrink-0 items-center gap-1 text-xs text-brand">{{ expanded ? '收起' : '展开' }}<svg class="h-3 w-3 transition-transform" :class="{'rotate-180':expanded}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg></span>
    </button>
    <div :id="id" class="mobile-section-body min-h-0 min-w-0 space-y-3 sm:space-y-4"><slot /></div>
  </div>
</template>

<style scoped>
@media (max-width: 639px), (max-width: 1023px) and (max-height: 500px) {
  .mobile-section-toggle { display: flex; }
  .mobile-section:not(.mobile-section-expanded):not(.mobile-section-fixed) > .mobile-section-body { display: none; }
  .mobile-section-expanded > .mobile-section-body { margin-top: 0.5rem; }
}
</style>
