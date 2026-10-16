<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import http from '@/api'
import ModalDialog from '@/components/ModalDialog.vue'
import PrivateImage from '@/components/PrivateImage.vue'

const props = defineProps({
  open: Boolean, row: { type: Object, default: null },
  kind: { type: String, default: 'source' }, statusLabel: { type: String, default: '' },
  allowProcess: Boolean, processDisabled: Boolean,
})
const emit = defineEmits(['update:open', 'process'])
const detail = ref(null), detailLoading = ref(false), detailError = ref('')
let request = 0
const formatSize = size => size == null ? '大小未知' : size >= 1073741824 ? `${(size / 1073741824).toFixed(2)} GB` : `${(size / 1048576).toFixed(1)} MB`
const metadata = computed(() => detail.value?.metadata || {})
const hasMetadata = computed(() => ['title', 'cn_title', 'casts', 'outline', 'banner', 'poster', 'local_banner', 'release_date'].some(key => metadata.value[key]))
const splitTags = value => (value || '').split(/[,，]/).map(s => s.trim()).filter(Boolean)
const detailFields = computed(() => [
  ['发行日期', metadata.value.release_date], ['时长', metadata.value.duration ? (/^\d+$/.test(metadata.value.duration) ? `${metadata.value.duration} 分钟` : metadata.value.duration) : ''],
  ['制作商', metadata.value.producer], ['发行商', metadata.value.publisher],
  ['系列', metadata.value.series], ['评分', metadata.value.star],
])

async function load() {
  const id = ++request
  detail.value = null; detailError.value = ''; detailLoading.value = true
  try {
    const endpoint = props.kind === 'link' ? '/medialinks/detail' : '/scrape/videos/detail'
    const params = props.kind === 'link' ? { link_path: props.row.link_path } : { path: props.row.path }
    const result = await http.get(endpoint, { params })
    if (id === request) detail.value = result
  } catch (e) { if (id === request) detailError.value = e.message }
  finally { if (id === request) detailLoading.value = false }
}
watch(() => [props.open, props.row], ([open, row]) => { if (open && row) load(); else request++ }, { immediate: true })
onBeforeUnmount(() => { request++ })
</script>

<template>
    <ModalDialog :open="open" @close="emit('update:open', false)" title="刮削资料" size="2xl" scrollable>
      <div v-if="detailLoading" class="space-y-4 py-8" role="status"><div class="h-6 w-1/2 animate-pulse rounded bg-gray-800"></div><div class="h-32 animate-pulse rounded bg-gray-800/60"></div><p class="text-xs text-gray-500">正在读取已保存的资料…</p></div>
      <div v-else-if="detailError" class="space-y-3 py-6"><p class="text-sm text-red-400" role="alert">{{ detailError }}</p><button class="btn-ghost" @click="load">重新加载</button></div>
      <div v-else-if="detail" class="space-y-6">
        <div class="flex flex-col gap-5 sm:flex-row">
          <div class="aspect-[2/3] w-36 shrink-0 overflow-hidden rounded-lg border border-gray-800 bg-gray-900 sm:w-44"><PrivateImage :src="detail.images?.poster" :alt="detail.code" img-class="h-full w-full object-cover" fallback-text="暂无封面" /></div>
          <div class="min-w-0 flex-1 space-y-3">
            <div class="flex flex-wrap items-center gap-2"><span class="font-mono text-lg font-semibold text-brand">{{ detail.code || '尚未识别番号' }}</span><span v-if="statusLabel" class="rounded bg-gray-800 px-2 py-1 text-xs text-gray-400">{{ statusLabel }}</span></div>
            <h3 class="break-words text-lg font-medium leading-relaxed text-gray-200">{{ metadata.cn_title || metadata.title || detail.name }}</h3>
            <p v-if="metadata.cn_title && metadata.title" class="text-xs leading-relaxed text-gray-500">{{ metadata.title }}</p>
            <p v-if="!hasMetadata" class="rounded-lg border border-dashed border-gray-700 p-3 text-sm text-gray-500">{{ detail.code ? '暂无已保存的刮削资料，可手动刮削获取标题、演员和图片。' : '先指定正确番号，再获取这部视频的资料。' }}</p>
            <dl v-else class="grid grid-cols-2 gap-x-4 gap-y-3"><div v-for="[label, value] in detailFields" :key="label"><dt class="text-[11px] text-gray-500">{{ label }}</dt><dd class="mt-1 break-words text-xs text-gray-300">{{ value || '—' }}</dd></div></dl>
          </div>
        </div>
        <div v-if="metadata.casts" class="space-y-2"><h4 class="text-xs text-gray-500">演员</h4><div class="flex flex-wrap gap-2"><span v-for="name in splitTags(metadata.casts)" :key="name" class="rounded-full bg-brand/10 px-3 py-1 text-xs text-brand">{{ name }}</span></div></div>
        <div v-if="metadata.genres" class="flex flex-wrap gap-2"><span v-for="tag in splitTags(metadata.genres)" :key="tag" class="rounded bg-gray-800 px-2 py-1 text-[11px] text-gray-400">{{ tag }}</span></div>
        <div v-if="metadata.outline" class="space-y-2"><h4 class="text-xs text-gray-500">简介</h4><p class="whitespace-pre-line text-sm leading-7 text-gray-300">{{ metadata.outline }}</p></div>
        <details v-if="detail.images?.fanart || detail.images?.stills?.length" class="space-y-3"><summary class="cursor-pointer text-xs text-gray-400">查看完整封面与剧照</summary><PrivateImage :src="detail.images.fanart" alt="完整封面" img-class="w-full rounded-lg" /><div class="grid grid-cols-2 gap-2"><PrivateImage v-for="(url, i) in detail.images.stills || []" :key="url" :src="url" :alt="`剧照 ${i + 1}`" img-class="w-full rounded" /></div></details>
        <div class="space-y-3 rounded-lg border border-gray-800 bg-gray-900/50 p-4 text-xs"><p class="text-gray-500">源视频 <span class="ml-2 text-gray-400">{{ formatSize(detail.size) }}</span></p><p class="break-all font-mono text-gray-400">{{ detail.path }}</p><template v-if="detail.links.length"><p class="text-gray-500">已登记入库位置</p><p v-for="path in detail.links" :key="path" class="break-all font-mono text-gray-400">{{ path }}</p></template><p v-if="detail.error" class="break-all text-red-400">{{ detail.error }}</p></div>
        <div class="flex flex-wrap items-center justify-between gap-3 border-t border-gray-800 pt-4"><p class="text-[11px] text-gray-500">显示已保存资料；再次刮削可更新元数据。</p><button class="btn-primary" v-if="allowProcess" :disabled="processDisabled" @click="emit('process', detail)">{{ !detail.code ? '指定番号并刮削' : detail.links.length ? '再次刮削' : '手动刮削' }}</button></div>
      </div>
    </ModalDialog>
</template>
