<script setup>
import { computed, ref } from 'vue'
import PrivateImage from '@/components/PrivateImage.vue'
import { useToast } from '@/composables/useToast'
import { downloadCode, precheckDownload } from '@/api'

const props = defineProps({
  row: { type: Object, required: true },
  sourceName: { type: String, default: '' },
  sectionName: { type: String, default: '' },
})
const toast = useToast()
const galleryOpen = ref(false)
const pushing = ref(false), pending = ref(null), downloadError = ref('')
const isWebUrl = value => /^https?:\/\//i.test(value || '')
const images = computed(() => [...new Set(props.row.previews || [])].filter(isWebUrl).slice(0, 40))
const imageSrc = url => `/api/v1/resources/${encodeURIComponent(props.row.key)}/image?${new URLSearchParams({ index: String(props.row.previews.indexOf(url)), token: localStorage.getItem('token') || '' })}`
const links = computed(() => [...(props.row.magnets || []), ...(props.row.ed2k || [])].slice(0, 100))
const size = computed(() => {
  const mb = Number(props.row.size_mb)
  return mb > 0 ? (mb >= 1024 ? `${(mb / 1024).toFixed(2)} GiB` : `${mb.toFixed(0)} MiB`) : '体积未知'
})
async function copy(value) {
  try { await navigator.clipboard.writeText(value); toast.success('链接已复制') }
  catch { toast.error('无法复制，请手动选择链接') }
}
async function download(link, force = false) {
  if (pushing.value || !props.row.code) return
  pushing.value = true
  downloadError.value = ''
  try {
    if (!force) {
      const check = await precheckDownload(props.row.code)
      if (check?.blocked) { pending.value = { link, reason: check.reason }; return }
    }
    await downloadCode({ code: props.row.code, title: props.row.title, site: 'BT', download_url: link, downloader: 'qbittorrent', force })
    pending.value = null
    toast.success('已推送到 qBittorrent')
  } catch (e) { downloadError.value = e.message }
  finally { pushing.value = false }
}
</script>

<template>
  <article class="card min-w-0 space-y-2">
    <div class="flex items-start gap-3">
      <div class="h-28 w-24 shrink-0 overflow-hidden rounded bg-black/20 sm:h-36 sm:w-40">
        <PrivateImage :src="images.length ? imageSrc(images[0]) : ''" :alt="row.title" img-class="h-full w-full object-contain" referrerpolicy="no-referrer" fallback-text="暂无预览图" />
      </div>
      <div class="min-w-0 flex-1 space-y-2">
        <p class="break-words text-sm font-medium">{{ row.title }}</p>
        <RouterLink v-if="row.code" class="inline-block text-xs text-brand" :to="{ name: 'Search', query: { keyword: row.code } }">{{ row.code }} · 搜索下载</RouterLink>
        <p v-else class="text-xs text-gray-500">未确认单一番号，仅供手动查看</p>
        <div class="flex flex-wrap gap-x-3 gap-y-1 text-xs text-gray-400">
          <span>{{ sourceName || row.source }}</span>
          <span v-if="sectionName || row.category">{{ [sectionName, row.category].filter(Boolean).join(' · ') }}</span>
          <span>{{ size }}</span>
          <span>发布 {{ row.published || '日期未知' }}</span>
        </div>
        <div class="flex flex-wrap gap-3 text-xs text-gray-500">
          <span>磁链 {{ row.magnets?.length || 0 }} · ED2K {{ row.ed2k?.length || 0 }} · 图片 {{ images.length }}</span>
          <a v-if="isWebUrl(row.detail_url)" :href="row.detail_url" target="_blank" rel="noreferrer" class="text-brand">查看原帖 ↗</a>
        </div>
        <button v-if="row.code && row.magnets?.length" class="btn-primary text-xs" :disabled="pushing" @click="download(row.magnets[0])">{{ pushing ? '推送中…' : 'qBittorrent 下载' }}</button>
        <p v-else-if="row.magnets?.length" class="text-xs text-gray-500">未确认番号，可复制磁链到下载器</p>
      </div>
    </div>
    <p v-if="downloadError" class="text-xs text-red-400" role="alert">{{ downloadError }}</p>
    <div v-if="pending" class="flex flex-wrap items-center gap-2 text-xs text-amber-400" role="alert">
      <span>{{ pending.reason }}，是否仍要下载？</span>
      <button class="btn-ghost text-xs" :disabled="pushing" @click="download(pending.link, true)">仍要下载</button>
      <button class="btn-ghost text-xs" :disabled="pushing" @click="pending = null">取消</button>
    </div>
    <details v-if="images.length" class="text-xs text-gray-400" @toggle="galleryOpen = $event.target.open">
      <summary class="cursor-pointer">展开图片预览（{{ images.length }} 张）</summary>
      <div v-if="galleryOpen" class="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
        <PrivateImage v-for="url in images" :key="url" :src="imageSrc(url)" :alt="row.title" img-class="max-h-[36rem] w-full rounded object-contain" referrerpolicy="no-referrer" fallback-text="图片暂时无法加载，可查看原帖" />
      </div>
    </details>
    <details v-if="links.length" class="text-xs text-gray-400">
      <summary class="cursor-pointer">资源链接</summary>
      <div v-for="link in links" :key="link" class="mt-2 flex flex-wrap items-start gap-2"><code class="min-w-0 flex-1 break-all">{{ link }}</code><button class="btn-ghost shrink-0 text-xs" @click="copy(link)">复制</button><button v-if="row.code && link.startsWith('magnet:')" class="btn-ghost text-xs" :disabled="pushing" @click="download(link)">qB 下载</button></div>
    </details>
  </article>
</template>
