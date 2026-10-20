<script setup>
defineProps({ contentClass: { type: String, default: 'space-y-4' } })
</script>

<template>
  <section class="page-panel flex h-full min-h-0 min-w-0 flex-col gap-4 overflow-hidden">
    <div v-if="$slots.header" class="max-h-[45%] shrink-0 space-y-4 overflow-auto overscroll-contain">
      <slot name="header" />
    </div>
    <div class="page-panel-content min-h-0 min-w-0 flex-1 overflow-auto overscroll-contain" :class="contentClass" tabindex="0" aria-label="页面内容">
      <slot />
    </div>
  </section>
</template>
