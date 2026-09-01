<script setup>
/**
 * 受隐私模式管辖的图片。
 *
 * 存在的理由：隐私模式靠给每个 <img> 挂 configStore.imageClass 实现，
 * 全靠写页面的人记得挂 —— 漏一处，那个页面在「模糊/隐藏」模式下就
 * 照样把图明晃晃地放出来。刮削试算的弹窗就漏过一次。
 *
 * 所以把「挂 class」这件事收进组件：页面用 <PrivateImage> 代替 <img>，
 * 遮挡是默认行为，不需要任何额外动作。
 *
 * 站内所有展示影片封面、剧照、演员头像的地方都该用它。确实不该被遮的
 * 图（图标、占位图、二维码之类与隐私无关的）继续用原生 <img> ——
 * 那不是漏，是本来就不该管。
 */
import { computed, ref, watch } from 'vue'
import { useConfigStore } from '@/stores/config'

const props = defineProps({
  src: { type: String, default: '' },
  alt: { type: String, default: '' },
  // 透传给 <img> 的 class。隐私模式那一份由组件自己加，不用写
  imgClass: { type: [String, Array, Object], default: '' },
  // 双拼封面要偏到人像那半边，值来自 code.portrait_side
  objectPosition: { type: String, default: '' },
  loading: { type: String, default: 'lazy' },
  title: { type: String, default: '' },
  // 加载失败时显示的文字。留空则失败后什么都不显示
  fallbackText: { type: String, default: '' },
})

const emit = defineEmits(['error', 'click'])

const configStore = useConfigStore()
const failed = ref(false)

// src 变了要重置失败状态，否则换了图还顶着上一张的错误。
// 用 watch 而不是在 computed 里改状态 —— computed 应当无副作用，
// 在里面写 ref 会让求值次数影响结果
watch(() => props.src, () => { failed.value = false })

const style = computed(() =>
  props.objectPosition ? { objectPosition: props.objectPosition } : undefined,
)

function onError(event) {
  failed.value = true
  emit('error', event)
}
</script>

<template>
  <img
    v-if="src && !failed"
    :src="src"
    :alt="alt"
    :loading="loading"
    :title="title || undefined"
    :class="[imgClass, configStore.imageClass]"
    :style="style"
    @error="onError"
    @click="emit('click', $event)"
  >
  <!-- 没有图或加载失败。占位交给调用方决定长什么样，
       给了 fallbackText 就用默认样式，否则走插槽 -->
  <slot v-else name="fallback">
    <div
      v-if="fallbackText"
      class="flex h-full w-full items-center justify-center text-[10px] text-gray-600"
    >
      {{ fallbackText }}
    </div>
  </slot>
</template>
