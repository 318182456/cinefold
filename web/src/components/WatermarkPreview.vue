<script setup>
import { ref, watch, onUnmounted } from 'vue'
import { previewScrapeWatermark } from '@/api'
const props = defineProps({ config: { type: Object, default: () => ({}) }, filename: { type: String, default: 'SONE-725-C-4K.mp4' }, code: { type: String, default: 'SONE-725' } })
const kind = ref('poster')
const showAll = ref(false)
const image = ref('')
const error = ref('')
let timer, generation = 0
watch(() => [props.config, props.filename, props.code, kind.value, showAll.value], () => {
  clearTimeout(timer)
  const current = ++generation
  timer = setTimeout(async () => {
    try {
      const data = await previewScrapeWatermark({ config: props.config, kind: kind.value, show_all: showAll.value, filename: props.filename, code: props.code })
      if (current !== generation) return
      image.value = data.image
      error.value = ''
    } catch (err) {
      if (current === generation) error.value = err.message || '预览失败'
    }
  }, 300)
}, { deep: true, immediate: true })
onUnmounted(() => { clearTimeout(timer); generation++ })
</script>

<template>
  <div class="space-y-2">
    <div class="flex flex-wrap items-center gap-3 text-xs text-gray-400">
      <select v-model="kind" class="input w-auto" aria-label="预览图片类型"><option value="poster">海报</option><option value="thumb">缩略图</option><option value="fanart">背景图</option></select>
      <label class="flex items-center gap-2"><input v-model="showAll" type="checkbox" />显示全部标记</label>
    </div>
    <p class="text-[11px] text-gray-500">位置示意，使用实际绘制规则。背景仅供预览；水印启用且选中此图片类型时显示角标。</p>
    <p v-if="error" class="text-xs text-amber-400">{{ error }}</p>
    <img v-else-if="image" :src="image" alt="水印位置预览" class="max-h-80 max-w-full rounded border border-gray-700" />
  </div>
</template>
