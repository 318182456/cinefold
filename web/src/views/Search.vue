<script setup>
import { ref } from 'vue'
import { searchCodes, searchTorrents, subscribeCode, downloadCode } from '@/api'
import { useToast } from '@/composables/useToast'
import CodeCard from '@/components/CodeCard.vue'
import EmptyState from '@/components/EmptyState.vue'

const toast = useToast()
const keyword = ref('')
const results = ref([])
const torrents = ref([])
const currentCode = ref('')
const searching = ref(false)
const seeking = ref(false)
const searched = ref(false)

async function search() {
  const value = keyword.value.trim()
  if (!value) return

  searching.value = true
  searched.value = true
  torrents.value = []
  try {
    const data = await searchCodes(value)
    results.value = data.items || []
    if (!results.value.length) toast.info('本地与远程都没有找到')
  } catch (err) {
    toast.error(err.message)
  } finally {
    searching.value = false
  }
}

async function findTorrents(code) {
  currentCode.value = code
  seeking.value = true
  torrents.value = []
  try {
    const data = await searchTorrents(code)
    torrents.value = data.items || []
    if (!torrents.value.length) toast.info('没有搜到符合过滤条件的种子')
  } catch (err) {
    toast.error(err.message)
  } finally {
    seeking.value = false
  }
}

async function pushTorrent(torrent) {
  try {
    await downloadCode({
      code: currentCode.value,
      download_url: torrent.download_url,
      site: torrent.site,
    })
    toast.success('已推送到下载器')
  } catch (err) {
    toast.error(err.message)
  }
}

async function quickSubscribe() {
  const value = keyword.value.trim()
  if (!value) return
  try {
    await subscribeCode(value)
    toast.success(`已订阅 ${value}`)
    search()
  } catch (err) {
    toast.error(err.message)
  }
}

const sizeText = (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(0)} MB`)
</script>

<template>
  <div class="space-y-5">
    <div class="flex gap-2">
      <input
        v-model="keyword"
        class="input flex-1"
        placeholder="输入番号，如 SSIS-001"
        @keyup.enter="search"
      />
      <button class="btn-primary" :disabled="searching" @click="search">搜索</button>
      <button class="btn-ghost hidden sm:inline-flex" @click="quickSubscribe">直接订阅</button>
    </div>

    <!-- 番号结果 -->
    <div v-if="results.length" class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      <div v-for="item in results" :key="item.code" class="space-y-2">
        <CodeCard :item="item" @changed="search" />
        <button class="btn-ghost w-full py-1.5 text-xs" @click="findTorrents(item.code)">
          搜索可用资源
        </button>
      </div>
    </div>

    <EmptyState
      v-else-if="searched && !searching"
      text="没有找到相关番号"
      hint="确认番号格式，或检查资源站是否可访问"
    />

    <!-- 种子列表 -->
    <section v-if="seeking || torrents.length" class="space-y-3">
      <h2 class="text-sm font-medium text-gray-300">
        {{ currentCode }} 的可用资源
        <span v-if="torrents.length" class="text-gray-500">（{{ torrents.length }}）</span>
      </h2>

      <p v-if="seeking" class="text-sm text-gray-500">正在搜索各站点…</p>

      <div v-else class="overflow-x-auto">
        <table class="w-full min-w-[640px] text-sm">
          <thead>
            <tr class="border-b border-gray-800 text-left text-xs text-gray-500">
              <th class="pb-2 pr-3 font-medium">标题</th>
              <th class="pb-2 pr-3 font-medium">站点</th>
              <th class="pb-2 pr-3 font-medium">大小</th>
              <th class="pb-2 pr-3 font-medium">做种</th>
              <th class="pb-2 pr-3 font-medium">标记</th>
              <th class="pb-2 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(torrent, index) in torrents"
              :key="`${torrent.site}-${torrent.id}-${index}`"
              class="border-b border-gray-800/60"
            >
              <td class="max-w-md truncate py-2 pr-3 text-gray-300" :title="torrent.title">
                {{ torrent.title }}
              </td>
              <td class="py-2 pr-3 text-gray-400">{{ torrent.site }}</td>
              <td class="py-2 pr-3 tabular-nums text-gray-400">{{ sizeText(torrent.size_mb) }}</td>
              <td class="py-2 pr-3 tabular-nums text-gray-400">{{ torrent.seeders }}</td>
              <td class="py-2 pr-3">
                <span v-if="torrent.free" class="badge mr-1 bg-emerald-900 text-emerald-300">免费</span>
                <span v-if="torrent.chinese" class="badge mr-1 bg-blue-900 text-blue-300">中字</span>
                <span v-if="torrent.uhd" class="badge mr-1 bg-purple-900 text-purple-300">4K</span>
                <span v-if="torrent.uc" class="badge bg-amber-900 text-amber-300">无码</span>
              </td>
              <td class="py-2">
                <button class="btn-primary px-2.5 py-1 text-xs" @click="pushTorrent(torrent)">
                  下载
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
