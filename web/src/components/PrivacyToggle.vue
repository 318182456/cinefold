<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useConfigStore } from '@/stores/config'
import { useToast } from '@/composables/useToast'

const configStore = useConfigStore()
const toast = useToast()

const open = ref(false)
const root = ref(null)

// 图标画的是「眼睛」的三种状态：睁眼 / 眯眼 / 闭眼划线
const MODES = [
  {
    key: 'VISIBLE',
    label: '显示',
    hint: '正常显示所有图片',
    icon: 'M15 12a3 3 0 11-6 0 3 3 0 016 0z M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z',
  },
  {
    key: 'BLUR',
    label: '模糊',
    hint: '图片打码，鼠标悬停时看清',
    icon: 'M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z M9.5 12h.01 M12 12h.01 M14.5 12h.01',
  },
  {
    key: 'INVISIBLE',
    label: '隐藏',
    hint: '图片全部遮黑，鼠标悬停时看清',
    icon: 'M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21',
  },
]

function modeOf(key) {
  return MODES.find((item) => item.key === key) || MODES[1]
}

async function pick(key) {
  open.value = false
  if (key === configStore.imageMode) return
  try {
    await configStore.setImageMode(key)
    toast.success(`图片已切到「${modeOf(key).label}」`)
  } catch (err) {
    toast.error(err.message)
  }
}

// 点别处收起菜单
function onClickOutside(event) {
  if (open.value && root.value && !root.value.contains(event.target)) open.value = false
}
function onKeydown(event) {
  if (event.key === 'Escape') open.value = false
}

onMounted(() => {
  document.addEventListener('click', onClickOutside)
  window.addEventListener('keydown', onKeydown)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onClickOutside)
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div ref="root" class="relative">
    <button
      class="flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs transition-colors"
      :class="
        configStore.imageMode === 'VISIBLE'
          ? 'text-gray-400 hover:bg-gray-800 hover:text-gray-200'
          : 'bg-brand/10 text-brand hover:bg-brand/20'
      "
      :title="`隐私模式：${modeOf(configStore.imageMode).hint}`"
      @click="open = !open"
    >
      <svg class="h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24">
        <path
          stroke-linecap="round"
          stroke-linejoin="round"
          :d="modeOf(configStore.imageMode).icon"
        />
      </svg>
      <span class="hidden sm:inline">{{ modeOf(configStore.imageMode).label }}</span>
    </button>

    <Transition
      enter-active-class="transition duration-100"
      enter-from-class="opacity-0"
      leave-active-class="transition duration-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="open"
        class="absolute right-0 z-30 mt-1 w-44 overflow-hidden rounded-lg border border-gray-800 bg-gray-900 py-1 shadow-xl"
      >
        <p class="px-3 pb-1 pt-1.5 text-[11px] font-medium uppercase tracking-wider text-gray-600">
          隐私模式
        </p>
        <button
          v-for="mode in MODES"
          :key="mode.key"
          class="flex w-full items-start gap-2.5 px-3 py-2 text-left transition-colors"
          :class="
            mode.key === configStore.imageMode
              ? 'bg-brand/10 text-brand'
              : 'text-gray-300 hover:bg-gray-800'
          "
          @click="pick(mode.key)"
        >
          <svg class="mt-0.5 h-4 w-4 shrink-0" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" :d="mode.icon" />
          </svg>
          <span class="min-w-0">
            <span class="block text-xs font-medium">{{ mode.label }}</span>
            <span class="block text-[11px] leading-tight text-gray-500">{{ mode.hint }}</span>
          </span>
        </button>
      </div>
    </Transition>
  </div>
</template>
