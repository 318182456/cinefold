<script setup>
import { ref, onMounted } from 'vue'
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
// 搜过种子就一直留着结果区块，空结果时也要留住「重新搜索」按钮
const sought = ref(false)

// 检索履历。只存在浏览器本地，不上报服务端 —— 搜索词属于个人痕迹，
// 也没有跨设备同步的必要
const HISTORY_KEY = 'search_history'
const HISTORY_MAX = 12
const history = ref([])

onMounted(() => {
  try {
    const saved = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]')
    // 存量数据可能被手工改坏，只收字符串
    history.value = Array.isArray(saved)
      ? saved.filter((s) => typeof s === 'string').slice(0, HISTORY_MAX)
      : []
  } catch {
    history.value = []
  }
})

function persistHistory() {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history.value))
  } catch {
    // 隐私模式下 localStorage 可能不可写，履历丢了不影响搜索
  }
}

function pushHistory(value) {
  // 重复搜同一个词时提到最前，而不是堆一串一样的
  history.value = [value, ...history.value.filter((v) => v !== value)].slice(0, HISTORY_MAX)
  persistHistory()
}

function removeHistory(value) {
  history.value = history.value.filter((v) => v !== value)
  persistHistory()
}

function clearHistory() {
  history.value = []
  persistHistory()
}

function searchFromHistory(value) {
  keyword.value = value
  search()
}

async function search() {
  const value = keyword.value.trim()
  if (!value) return

  searching.value = true
  searched.value = true
  torrents.value = []
  sought.value = false
  try {
    const data = await searchCodes(value)
    results.value = data.items || []
    // 搜过就记，搜不到的词同样值得留着改一改再试
    pushHistory(value)
    if (!results.value.length) toast.info('本地与远程都没有找到')
  } catch (err) {
    toast.error(err.message)
  } finally {
    searching.value = false
  }
}

async function findTorrents(code, refresh = false) {
  currentCode.value = code
  seeking.value = true
  sought.value = true
  torrents.value = []
  try {
    const data = await searchTorrents(code, refresh)
    torrents.value = data.items || []
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

    <!-- 检索履历 -->
    <div v-if="history.length" class="flex flex-wrap items-center gap-2">
      <span class="text-xs text-gray-500">最近搜索</span>
      <span
        v-for="item in history"
        :key="item"
        class="group inline-flex items-center gap-1 rounded bg-gray-800 py-1 pl-2 pr-1 text-xs text-gray-300"
      >
        <button class="hover:text-white" @click="searchFromHistory(item)">{{ item }}</button>
        <button
          class="px-1 text-gray-600 hover:text-gray-300"
          :title="`删除 ${item}`"
          @click.stop="removeHistory(item)"
        >
          ×
        </button>
      </span>
      <button class="text-xs text-gray-600 hover:text-gray-400" @click="clearHistory">
        清空
      </button>
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
    <section v-if="sought" class="space-y-3">
      <div class="flex items-center gap-3">
        <h2 class="text-sm font-medium text-gray-300">
          {{ currentCode }} 的可用资源
          <span v-if="torrents.length" class="text-gray-500">（{{ torrents.length }}）</span>
        </h2>
        <button
          class="btn-ghost px-2 py-1 text-xs"
          :disabled="seeking"
          title="跳过 30 分钟的检索缓存，直接重新搜各站点"
          @click="findTorrents(currentCode, true)"
        >
          重新搜索
        </button>
      </div>

      <p v-if="seeking" class="text-sm text-gray-500">正在搜索各站点…</p>

      <!-- 检索结果缓存 30 分钟，命中空缓存时点上面的「重新搜索」是唯一的出路 -->
      <p v-else-if="!torrents.length" class="text-sm text-gray-500">
        各站点都没有这个番号的种子，可点「重新搜索」跳过缓存再试
      </p>

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
