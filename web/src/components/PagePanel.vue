<script setup>
import MobileSection from './MobileSection.vue'
defineProps({
  contentClass: { type: String, default: 'space-y-4' },
  collapseHeader: { type: Boolean, default: true },
  headerLabel: { type: String, default: '筛选与统计' },
  mobileSummary: { type: String, default: '' },
})
</script>

<template>
  <section class="page-panel flex h-full min-h-0 min-w-0 flex-col gap-2 overflow-hidden sm:gap-4">
    <div v-if="$slots.navigation" class="min-w-0 shrink-0"><slot name="navigation" /></div>
    <MobileSection v-if="$slots.header" class="page-panel-header flex max-h-[45%] min-h-0 shrink-0 flex-col" :collapsible="collapseHeader" :title="headerLabel" :summary="mobileSummary">
      <slot name="header" />
    </MobileSection>
    <div class="page-panel-content min-h-0 min-w-0 flex-1 overflow-auto overscroll-contain" :class="contentClass" tabindex="0" aria-label="页面内容">
      <slot />
    </div>
  </section>
</template>
