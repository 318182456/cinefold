<script setup>
import { onBeforeUnmount, watch } from 'vue'
import { useConfigStore } from '@/stores/config'

const props = defineProps({
  src: { type: String, default: '' },
  alt: { type: String, default: '' },
})
const emit = defineEmits(['close'])

const configStore = useConfigStore()

function onKeydown(event) {
  if (event.key === 'Escape') emit('close')
}

// 打开时才挂键盘监听，免得列表页上几十张卡片各挂一个
watch(
  () => props.src,
  (src) => {
    if (src) {
      window.addEventListener('keydown', onKeydown)
      // 灯箱盖住整屏时底下的列表不该还能滚
      document.body.style.overflow = 'hidden'
    } else {
      window.removeEventListener('keydown', onKeydown)
      document.body.style.overflow = ''
    }
  },
)

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  document.body.style.overflow = ''
})
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div
        v-if="src"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
        @click="emit('close')"
      >
        <!-- 图本身不关闭，留给用户放大看细节。
             隐私模式在这儿照样生效 —— 全屏大图才是最怕被旁人瞥见的，
             鼠标移上去（触屏点一下）就看清 -->
        <img
          :src="src"
          :alt="alt"
          class="max-h-full max-w-full rounded-lg object-contain shadow-2xl"
          :class="configStore.imageClass"
          @click.stop
        />
        <button
          class="absolute right-4 top-4 rounded-full bg-black/50 px-3 py-1 text-sm text-gray-200 hover:bg-black/70"
          @click.stop="emit('close')"
        >
          关闭
        </button>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.15s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
