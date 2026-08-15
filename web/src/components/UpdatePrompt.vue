<script setup>
import { onMounted, onUnmounted, ref } from 'vue'

// Service Worker 装好新版本后由 utils/pwa.js 派发。
// 不自动刷新：用户可能正在填表单，刷新会把输入弄丢，交给他自己点
const show = ref(false)

function onUpdated() {
  show.value = true
}

onMounted(() => window.addEventListener('sw:updated', onUpdated))
onUnmounted(() => window.removeEventListener('sw:updated', onUpdated))
</script>

<template>
  <Transition
    enter-active-class="transition duration-200"
    enter-from-class="translate-y-2 opacity-0"
    leave-active-class="transition duration-150"
    leave-to-class="opacity-0"
  >
    <div
      v-if="show"
      class="fixed inset-x-4 bottom-[calc(1rem+env(safe-area-inset-bottom))] z-[100] mx-auto flex max-w-md items-center gap-3 rounded-lg border border-gray-700 bg-gray-900/95 px-4 py-3 text-sm shadow-lg backdrop-blur"
    >
      <span class="flex-1 text-gray-200">有新版本，刷新后生效</span>
      <button class="btn-primary px-3 py-1.5 text-xs" @click="location.reload()">刷新</button>
      <button
        class="rounded p-1 text-gray-500 hover:text-gray-300"
        title="稍后"
        @click="show = false"
      >
        <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
          <path stroke-linecap="round" d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>
    </div>
  </Transition>
</template>
