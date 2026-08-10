<script setup>
defineProps({
  text: { type: String, default: '暂无数据' },
  hint: { type: String, default: '' },
})
</script>

<template>
  <div class="flex flex-col items-center justify-center gap-2 py-16 text-center">
    <svg class="h-12 w-12 text-gray-700" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round"
        d="M20.25 7.5l-.625 10.632a2.25 2.25 0 01-2.247 2.118H6.622a2.25 2.25 0 01-2.247-2.118L3.75 7.5M10 11.25h4M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125z" />
    </svg>
    <p class="text-sm text-gray-500">{{ text }}</p>
    <p v-if="hint" class="text-xs text-gray-600">{{ hint }}</p>
  </div>
</template>
