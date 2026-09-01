<script setup>
/**
 * 弹窗外壳。
 *
 * 站内十来个弹窗原本各写一遍同样的遮罩 —— 一模一样的
 * "fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
 * 加 @click.self 关闭。复制粘贴出来的东西没法统一改，也容易漏：
 * 实测那十个里没有一个支持 Esc 关闭，打开时背景也照样能滚。
 *
 * 收进组件后这些行为默认就有，页面只管弹窗里的内容。
 */
import { onBeforeUnmount, onMounted, watch } from 'vue'

const props = defineProps({
  // v-model:open。用 open 而不是 v-if 包在外面，是为了让组件能在
  // 关闭时收尾（放开 body 滚动、摘掉键盘监听）
  open: { type: Boolean, default: false },
  // 标题。留空则不渲染标题栏（含关闭按钮），整块交给默认插槽 ——
  // 灯箱那种全屏看图的弹窗不需要标题栏
  title: { type: String, default: '' },
  // 弹窗宽度档位。照现有用法取的几档，避免每处自己写 max-w-*
  size: { type: String, default: 'md' },
  // 内容超高时是否可滚。长列表要开，短表单不必
  scrollable: { type: Boolean, default: false },
  // 点遮罩是否关闭。执行中的操作不该被误点打断
  closeOnBackdrop: { type: Boolean, default: true },
  // 按 Esc 是否关闭。同上
  closeOnEsc: { type: Boolean, default: true },
})

const emit = defineEmits(['update:open', 'close'])

const SIZES = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-2xl',
  '2xl': 'max-w-4xl',
  '3xl': 'max-w-5xl',
}

function close() {
  emit('update:open', false)
  emit('close')
}

function onBackdrop() {
  if (props.closeOnBackdrop) close()
}

function onKeydown(event) {
  if (event.key === 'Escape' && props.open && props.closeOnEsc) close()
}

// 弹窗打开时锁住 body 滚动。移动端尤其明显 —— 不锁的话背景会跟着滑，
// 手指想滚弹窗内容却滚了整页。与 MainLayout 抽屉的做法一致
watch(
  () => props.open,
  (open) => {
    document.body.classList.toggle('overflow-hidden', open)
  },
)

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  // 组件在打开状态下被销毁时（路由跳走），锁不能留在 body 上
  document.body.classList.remove('overflow-hidden')
})
</script>

<template>
  <Transition
    enter-active-class="transition duration-150"
    enter-from-class="opacity-0"
    leave-active-class="transition duration-150"
    leave-to-class="opacity-0"
  >
    <div
      v-if="open"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      @click.self="onBackdrop"
    >
      <div
        class="card w-full space-y-3"
        :class="[
          SIZES[size] || SIZES.md,
          scrollable ? 'max-h-[85vh] overflow-y-auto' : '',
        ]"
      >
        <!-- 标题栏。给了 title 就渲染，否则整块交给 default 插槽 -->
        <div v-if="title || $slots.title" class="flex items-start justify-between gap-3">
          <div class="min-w-0">
            <slot name="title">
              <p class="text-sm font-medium text-gray-200">{{ title }}</p>
            </slot>
          </div>
          <button class="btn-ghost shrink-0 px-2 py-1 text-xs" @click="close">关闭</button>
        </div>

        <slot />

        <div v-if="$slots.footer" class="flex flex-wrap justify-end gap-2">
          <slot name="footer" :close="close" />
        </div>
      </div>
    </div>
  </Transition>
</template>
