<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  batchDeleteMediaLinks, batchDropMediaLinkRecords, deleteMediaLink,
  dropMediaLinkRecord, getMediaLinkStats, listMediaLinkOrphans, listMediaLinks,
  previewMediaLinkDelete, pruneMediaLinks, recoverMediaLinks, registerMediaLink,
} from '@/api'
import { useToast } from '@/composables/useToast'
import LoadingBlock from '@/components/LoadingBlock.vue'

const toast = useToast()

const items = ref([])
const total = ref(0)
const page = ref(1)
// 每页条数可调：后端对当前页逐条探测文件是否存在，页越大越慢
const size = ref(20)
const SIZE_OPTIONS = [20, 50, 100]
const keyword = ref('')
const missingOnly = ref(false)
const loading = ref(false)

// 视图模式：all = 全部关联，orphan = 下载侧已删但媒体库仍在的那批。
// 两者数据源不同（后者要问下载器），共用搜索框与分页，切换时重置页码
const view = ref('all')
const isOrphan = computed(() => view.value === 'orphan')
const orphanSourceGone = ref(0)
const orphanTorrentGone = ref(0)

// 孤儿一览的多选。按 link_path 选而不是 code —— 同一番号可能有多条链接，
// 用户勾的是具体哪一条。不复用 useCodeSelection：那个是按 code 组织的，
// 动作也写死成订阅/取消订阅
const picked = ref(new Set())
const batching = ref(false)
// 批量删除的确认框。真删文件不可逆，必须再问一次
const batchConfirm = ref('')

const pickedCount = computed(() => picked.value.size)
const allPicked = computed(
  () => items.value.length > 0 && items.value.every((i) => picked.value.has(i.link_path)),
)

function isPicked(path) {
  return picked.value.has(path)
}

function togglePick(path) {
  // Set 原地改动不触发响应式，得换新实例
  const next = new Set(picked.value)
  next.has(path) ? next.delete(path) : next.add(path)
  picked.value = next
}

function togglePickAll() {
  const next = new Set(picked.value)
  const paths = items.value.map((i) => i.link_path)
  if (allPicked.value) paths.forEach((p) => next.delete(p))
  else paths.forEach((p) => next.add(p))
  picked.value = next
}

function clearPicked() {
  picked.value = new Set()
}
const stats = ref(null)
// 失效数要全表探测磁盘，NAS 上可能几十秒，跟总数分开请求单独渲染
const missing = ref(null)
const missingLoading = ref(false)
const deleteEnabled = ref(false)
const libraryPath = ref('')

// 手工登记
const registerOpen = ref(false)
const registerDraft = ref({ code: '', source_path: '', link_path: '' })
const registering = ref(false)

// 删除确认。必须先看过演练结果才能真删，避免误删
const confirming = ref(null)
const preview = ref(null)
const previewing = ref(false)
const deleting = ref(false)

const pages = computed(() => Math.max(1, Math.ceil(total.value / size.value)))

// 当前页覆盖的区间，显示成「第 1–20 条 / 共 41 条」
const rangeStart = computed(() => (total.value ? (page.value - 1) * size.value + 1 : 0))
const rangeEnd = computed(() => Math.min(page.value * size.value, total.value))

// 同一番号的多条链接归到一起显示，转种/多集时列表才不会散开
const grouped = computed(() => {
  const map = new Map()
  items.value.forEach((item) => {
    if (!map.has(item.code)) map.set(item.code, [])
    map.get(item.code).push(item)
  })
  return [...map.entries()].map(([code, links]) => ({ code, links }))
})

function fileName(path) {
  return (path || '').split(/[\\/]/).pop() || path
}

function shortTime(value) {
  if (!value) return ''
  return value.slice(0, 16).replace('T', ' ')
}

// 扣留中的记录还剩多久被删。与监控目录页的口径保持一致
function deleteLabel(hold) {
  const left = hold.seconds_left || 0
  if (left <= 0) return '下轮对账将删除'
  if (left >= 3600) return `${Math.ceil(left / 3600)} 小时后删除`
  if (left >= 60) return `${Math.ceil(left / 60)} 分钟后删除`
  return `${left} 秒后删除`
}

async function load() {
  loading.value = true
  try {
    // refresh 只在用户显式点「重新扫描」时为真：默认走后端缓存，
    // 否则翻一次页就要重拉一遍下载器全量种子清单
    const data = isOrphan.value
      ? await listMediaLinkOrphans({
          keyword: keyword.value.trim(),
          page: page.value,
          size: size.value,
        })
      : await listMediaLinks({
          keyword: keyword.value.trim(),
          missing_only: missingOnly.value,
          page: page.value,
          size: size.value,
        })
    items.value = data.items || []
    total.value = data.total || 0
    deleteEnabled.value = !!data.delete_enabled
    if (isOrphan.value) {
      orphanSourceGone.value = data.source_gone || 0
      orphanTorrentGone.value = data.torrent_gone || 0
    } else {
      libraryPath.value = data.library_path || ''
    }
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

// 重新扫描：绕过后端缓存，重新问一次下载器与磁盘
async function rescanOrphans() {
  loading.value = true
  try {
    const data = await listMediaLinkOrphans({
      keyword: keyword.value.trim(),
      page: page.value,
      size: size.value,
      refresh: true,
    })
    items.value = data.items || []
    total.value = data.total || 0
    orphanSourceGone.value = data.source_gone || 0
    orphanTorrentGone.value = data.torrent_gone || 0
    toast.success(`扫描完成，共 ${data.total || 0} 条`)
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

function switchView(next) {
  if (view.value === next) return
  view.value = next
  page.value = 1
  // 「只看已丢失」是全部视图的筛选，切到孤儿视图后不再适用
  if (next === 'orphan') missingOnly.value = false
  clearPicked()
  load()
}

// 纯 SQL 聚合，立刻返回
async function loadStats() {
  try {
    stats.value = await getMediaLinkStats(false)
  } catch {
    // 概览拿不到不影响列表
  }
}

// 全表磁盘探测，慢。单独跑，让前三个数先出来
async function loadMissing() {
  missingLoading.value = true
  try {
    missing.value = (await getMediaLinkStats(true)).missing
  } catch {
    missing.value = null
  } finally {
    missingLoading.value = false
  }
}

// 增删之后两份都要刷新
function reloadStats() {
  return Promise.all([loadStats(), loadMissing()])
}

function search() {
  page.value = 1
  // 选中的多半已被筛掉，留着就成了「删掉屏幕上看不见的东西」
  clearPicked()
  load()
}

function toggleMissing() {
  missingOnly.value = !missingOnly.value
  page.value = 1
  load()
}

function go(next) {
  if (next < 1 || next > pages.value || next === page.value) return
  page.value = next
  // 跨页保留选中很危险：点「批量删除」时删的是屏幕上看不到的记录。
  // 全选按钮也只作用于当前页，跨页累积会让「已选 N」与眼前所见对不上
  clearPicked()
  load()
}

function changeSize(next) {
  const value = Number(next)
  if (!value || value === size.value) return
  size.value = value
  page.value = 1
  clearPicked()
  load()
}

// ---------------------------------------------------------------- 登记
async function submitRegister() {
  const payload = {
    code: registerDraft.value.code.trim(),
    source_path: registerDraft.value.source_path.trim(),
    link_path: registerDraft.value.link_path.trim(),
  }
  if (!payload.code || !payload.source_path) {
    toast.error('番号与源文件路径不能为空')
    return
  }
  registering.value = true
  try {
    const data = await registerMediaLink(payload)
    toast.success(`已登记 ${(data.links || []).length} 条`)
    registerOpen.value = false
    registerDraft.value = { code: '', source_path: '', link_path: '' }
    await Promise.all([load(), reloadStats()])
  } catch (err) {
    toast.error(err.message)
  } finally {
    registering.value = false
  }
}

// ---------------------------------------------------------------- 删除
// 打开确认框时立刻跑一次演练，让用户看到确切的删除范围再决定
async function askDelete(group) {
  confirming.value = group
  preview.value = null
  previewing.value = true
  try {
    preview.value = await previewMediaLinkDelete({ code: group.code })
  } catch (err) {
    toast.error(err.message)
    confirming.value = null
  } finally {
    previewing.value = false
  }
}

async function confirmDelete() {
  const group = confirming.value
  deleting.value = true
  try {
    const data = await deleteMediaLink({ code: group.code, dry_run: false })
    if (data.dry_run) {
      // 后端因全局开关未启用降级成了演练
      toast.error('联动删除未启用，本次仅演练。请先在设置中开启')
    } else {
      const n =
        (data.files_deleted || []).length +
        (data.links_deleted || []).length +
        (data.sidecars_deleted || []).length
      const dirs = (data.dirs_deleted || []).length
      toast.success(
        `已删除 ${n} 个文件、${(data.torrents_deleted || []).length} 个种子` +
          (dirs ? `，清理 ${dirs} 个空目录` : ''),
      )
    }
    if ((data.errors || []).length) toast.error(data.errors.join('; '))
    confirming.value = null
    await Promise.all([load(), reloadStats()])
  } catch (err) {
    toast.error(err.message)
  } finally {
    deleting.value = false
  }
}

// ---------------------------------------------------------------- 批量
// 批量联动删除：删种 + 删源文件 + 删硬链接（Emby 里的那份）+ 清记录
async function runBatchDelete() {
  if (!pickedCount.value) return
  batching.value = true
  try {
    const data = await batchDeleteMediaLinks({
      link_paths: [...picked.value],
      dry_run: false,
    })
    if (data.dry_run) {
      toast.error('联动删除未启用，本次仅演练。请先在设置中开启')
    } else {
      toast.success(`已删除 ${data.deleted} / ${data.total} 条`)
    }
    // 部分失败照常展示明细，不掩盖
    if ((data.errors || []).length) {
      toast.error(`${data.errors.length} 条出错：${data.errors.slice(0, 2).join('; ')}`)
    }
    batchConfirm.value = ''
    clearPicked()
    await Promise.all([load(), reloadStats()])
  } catch (err) {
    toast.error(err.message)
  } finally {
    batching.value = false
  }
}

// 批量只删记录，不碰文件
async function runBatchDropRecords() {
  if (!pickedCount.value) return
  batching.value = true
  try {
    const data = await batchDropMediaLinkRecords([...picked.value])
    const missing = (data.missing || []).length
    toast.success(
      `已删除 ${(data.removed || []).length} 条记录` +
        (missing ? `，${missing} 条已不在库中` : ''),
    )
    batchConfirm.value = ''
    clearPicked()
    await Promise.all([load(), reloadStats()])
  } catch (err) {
    toast.error(err.message)
  } finally {
    batching.value = false
  }
}

async function dropRecord(item) {
  try {
    await dropMediaLinkRecord(item.link_path)
    toast.success('已删除记录')
    await Promise.all([load(), reloadStats()])
  } catch (err) {
    toast.error(err.message)
  }
}

// ---------------------------------------------------------------- 记录重建
// 误删记录后的补救：按 History.save_path 把关联配回来。
// 先演练看配对结果，确认无误再落库 —— 重建的是反向删除的依据
const recoverPreview = ref(null)
const recovering = ref(false)

async function askRecover() {
  recovering.value = true
  try {
    recoverPreview.value = await recoverMediaLinks(true)
  } catch (err) {
    toast.error(err.message)
  } finally {
    recovering.value = false
  }
}

async function confirmRecover() {
  recovering.value = true
  try {
    const data = await recoverMediaLinks(false)
    toast.success(`已重建 ${(data.recovered || []).length} 条关联`)
    recoverPreview.value = null
    await Promise.all([load(), reloadStats()])
  } catch (err) {
    toast.error(err.message)
  } finally {
    recovering.value = false
  }
}

async function prune() {
  try {
    const data = await pruneMediaLinks()
    toast.success(`已清理 ${(data.removed || []).length} 条失效记录`)
    await Promise.all([load(), reloadStats()])
  } catch (err) {
    toast.error(err.message)
  }
}

onMounted(() => {
  load()
  loadStats()
  loadMissing()
})
</script>

<template>
  <div class="space-y-4">
    <!-- 概览 -->
    <div class="grid gap-3 sm:grid-cols-4">
      <div class="card">
        <p class="text-xs text-gray-500">关联总数</p>
        <p class="mt-1 text-xl font-semibold text-gray-200">{{ stats?.total ?? '—' }}</p>
      </div>
      <div class="card">
        <p class="text-xs text-gray-500">涉及番号</p>
        <p class="mt-1 text-xl font-semibold text-gray-200">{{ stats?.codes ?? '—' }}</p>
      </div>
      <div class="card">
        <p class="text-xs text-gray-500">文件已丢失</p>
        <!-- 探测中显示占位，别让用户以为是 0 -->
        <p v-if="missingLoading" class="mt-1 text-xl font-semibold text-gray-500">检测中…</p>
        <p
          v-else
          class="mt-1 text-xl font-semibold"
          :class="missing ? 'text-amber-400' : 'text-gray-200'"
        >
          {{ missing ?? '—' }}
        </p>
      </div>
      <div class="card">
        <p class="text-xs text-gray-500">联动删除</p>
        <p
          class="mt-1 text-xl font-semibold"
          :class="deleteEnabled ? 'text-emerald-400' : 'text-gray-500'"
        >
          {{ deleteEnabled ? '已启用' : '未启用' }}
        </p>
      </div>
    </div>

    <!-- 未启用时说清楚删除按钮的实际行为，免得以为删了其实没删 -->
    <p v-if="!deleteEnabled" class="rounded-lg bg-amber-950/40 px-3 py-2 text-xs text-amber-300">
      联动删除未启用（MEDIALINK_DELETE_ENABLED=false），删除操作只会演练不会真的删文件。
      需要真删请先在设置中开启。
    </p>
    <p v-if="!libraryPath" class="rounded-lg bg-gray-800/60 px-3 py-2 text-xs text-gray-400">
      未配置媒体库根目录（MEDIALINK_LIBRARY_PATH），无法按 inode 反查硬链接，登记会失败。
    </p>

    <!-- 工具条 -->
    <div class="flex flex-wrap items-center gap-2">
      <input
        v-model="keyword"
        class="input max-w-xs"
        placeholder="搜番号或路径"
        @keyup.enter="search"
      />
      <button class="btn-ghost px-3 py-1.5 text-xs" @click="search">搜索</button>
      <button
        v-if="!isOrphan"
        class="btn-ghost px-3 py-1.5 text-xs"
        :class="missingOnly ? 'text-amber-300' : ''"
        @click="toggleMissing"
      >
        {{ missingOnly ? '显示全部' : '只看已丢失' }}
      </button>
      <button
        v-if="isOrphan"
        class="btn-ghost px-3 py-1.5 text-xs"
        @click="rescanOrphans"
      >
        重新扫描
      </button>
      <button
        v-if="!isOrphan"
        class="btn-ghost ml-auto px-3 py-1.5 text-xs"
        :disabled="recovering"
        @click="askRecover"
      >
        {{ recovering ? '扫描中…' : '重建缺失记录' }}
      </button>
      <button v-if="!isOrphan" class="btn-ghost px-3 py-1.5 text-xs" @click="prune">
        清理失效记录
      </button>
      <button
        v-if="!isOrphan"
        class="btn-primary px-3 py-1.5 text-xs"
        @click="registerOpen = true"
      >
        手工登记
      </button>
    </div>

    <!-- 视图切换。孤儿一览的数据源与全部关联不同（要问下载器），
         做成两个视图而不是一个筛选项 -->
    <div class="flex flex-wrap items-center gap-2">
      <button
        class="btn-ghost px-3 py-1.5 text-xs"
        :class="!isOrphan ? 'border-brand text-brand' : ''"
        @click="switchView('all')"
      >
        全部关联
      </button>
      <button
        class="btn-ghost px-3 py-1.5 text-xs"
        :class="isOrphan ? 'border-amber-400 text-amber-300' : ''"
        @click="switchView('orphan')"
      >
        下载侧已删 / Emby 仍在
      </button>
      <template v-if="isOrphan">
        <span v-if="orphanSourceGone" class="badge bg-red-950/60 text-red-300">
          源文件已删 {{ orphanSourceGone }}
        </span>
        <span v-if="orphanTorrentGone" class="badge bg-amber-950/60 text-amber-300">
          种子已删 {{ orphanTorrentGone }}
        </span>
      </template>
    </div>

    <LoadingBlock v-if="loading" :rows="5" />

    <!-- 孤儿一览：下载侧已删、媒体库侧仍在。
         不按番号分组 —— 每条都是要单独处置的问题记录，分组会把它藏起来 -->
    <template v-else-if="isOrphan">
      <p v-if="!items.length" class="card text-center text-sm text-gray-500">
        没有发现下载侧已删、媒体库侧仍在的关联。
      </p>

      <!-- 多选工具条。全选只作用于当前页，翻页会清空选中 -->
      <div v-if="items.length" class="flex flex-wrap items-center gap-2">
        <button class="btn-ghost px-3 py-1 text-xs" @click="togglePickAll">
          {{ allPicked ? '取消本页全选' : '本页全选' }}
        </button>
        <span class="text-xs tabular-nums text-gray-500">已选 {{ pickedCount }}</span>
        <button
          v-if="pickedCount"
          class="btn-ghost px-3 py-1 text-xs"
          :disabled="batching"
          @click="clearPicked"
        >
          清空选择
        </button>

        <button
          class="btn px-3 py-1 text-xs"
          :class="!pickedCount || batching ? 'btn-ghost' : 'bg-red-900 text-red-200'"
          :disabled="!pickedCount || batching"
          @click="batchConfirm = 'delete'"
        >
          批量联动删除
        </button>
        <button
          class="btn-ghost px-3 py-1 text-xs"
          :disabled="!pickedCount || batching"
          @click="batchConfirm = 'record'"
        >
          批量删记录
        </button>
      </div>

      <div v-if="items.length" class="space-y-2">
        <div
          v-for="item in items"
          :key="item.link_path"
          class="card space-y-2"
          :class="isPicked(item.link_path) ? 'border-brand' : ''"
        >
          <div class="flex flex-wrap items-center gap-2">
            <input
              type="checkbox"
              class="h-3.5 w-3.5 shrink-0 cursor-pointer accent-brand"
              :checked="isPicked(item.link_path)"
              @change="togglePick(item.link_path)"
            />
            <span class="font-mono text-sm font-medium text-brand">{{ item.code }}</span>
            <span v-if="item.source_gone" class="badge bg-red-950/60 text-red-300">
              源文件已删
            </span>
            <span v-if="item.torrent_gone" class="badge bg-amber-950/60 text-amber-300">
              种子已删
            </span>
            <button
              class="btn-ghost ml-auto px-2 py-0.5 text-[11px] text-red-400 hover:bg-red-950/40"
              @click="askDelete({ code: item.code, links: [item] })"
            >
              联动删除
            </button>
            <button
              class="btn-ghost px-2 py-0.5 text-[11px]"
              @click="dropRecord(item)"
            >
              删记录
            </button>
          </div>

          <div class="space-y-1 rounded-lg bg-gray-900/60 px-3 py-2">
            <!-- 媒体库侧：还在，所以 Emby 里仍看得到这个条目 -->
            <div class="flex items-start gap-2">
              <span
                class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500"
                title="媒体库文件仍在，Emby 里仍可见"
              />
              <div class="min-w-0 flex-1">
                <p class="truncate text-xs text-gray-300" :title="item.link_path">
                  {{ fileName(item.link_path) }}
                </p>
                <p class="truncate text-[11px] text-gray-600" :title="item.link_path">
                  {{ item.link_path }}
                </p>
              </div>
            </div>

            <div class="mt-1 border-t border-gray-800 pt-1">
              <div class="flex items-start gap-2">
                <span
                  class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full"
                  :class="item.source_gone ? 'bg-red-500' : 'bg-emerald-500'"
                  :title="item.source_gone ? '源文件已删除' : '源文件仍在'"
                />
                <p
                  class="min-w-0 flex-1 truncate text-[11px] text-gray-600"
                  :title="item.source_path"
                >
                  {{ item.source_path }}
                </p>
              </div>
            </div>
          </div>

          <div class="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-gray-600">
            <span>创建 {{ shortTime(item.create_time) || '未知' }}</span>
            <!-- 只删种未删文件时源文件还在，没有删除时刻可言 -->
            <span :class="item.delete_time ? 'text-red-400' : ''">
              删除 {{ shortTime(item.delete_time) || '—' }}
            </span>
            <span v-if="item.torrent_hashes && item.torrent_hashes.length">
              种子 {{ item.torrent_hashes.length }} 个
            </span>
          </div>
        </div>
      </div>
    </template>

    <p v-else-if="!grouped.length" class="card text-center text-sm text-gray-500">
      暂无硬链接关联。刮削工具回调 /webhook/scrape 后会自动登记。
    </p>

    <!-- 列表：按番号分组 -->
    <div v-else class="space-y-3">
      <div v-for="group in grouped" :key="group.code" class="card space-y-2">
        <div class="flex flex-wrap items-center gap-2">
          <span class="font-mono text-sm font-medium text-brand">{{ group.code }}</span>
          <!-- 长片名放不进 code 列，code 是哈希 —— 把原文件名显示出来，
               否则这一组根本认不出是哪部片子 -->
          <span
            v-if="group.links[0].filename"
            class="min-w-0 max-w-full truncate text-xs text-gray-400"
            :title="group.links[0].filename"
          >
            {{ group.links[0].filename }}
          </span>
          <span class="badge bg-gray-800 text-gray-400">
            {{ group.links.length }} 条链接
          </span>
          <span
            v-if="group.links[0].torrent_count"
            class="badge bg-gray-800 text-gray-400"
          >
            {{ group.links[0].torrent_count }} 个种子
          </span>
          <span class="text-[11px] text-gray-600">
            {{ shortTime(group.links[0].create_time) }}
          </span>
          <button
            class="btn-ghost ml-auto px-2 py-0.5 text-[11px] text-red-400 hover:bg-red-950/40"
            @click="askDelete(group)"
          >
            联动删除
          </button>
        </div>

        <div
          v-for="link in group.links"
          :key="link.link_path"
          class="space-y-1 rounded-lg bg-gray-900/60 px-3 py-2"
        >
          <div class="flex items-start gap-2">
            <span
              class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full"
              :class="link.link_exists ? 'bg-emerald-500' : 'bg-red-500'"
              :title="link.link_exists ? '硬链接存在' : '硬链接已丢失'"
            />
            <div class="min-w-0 flex-1">
              <p class="truncate text-xs text-gray-300" :title="link.link_path">
                {{ fileName(link.link_path) }}
              </p>
              <p class="truncate text-[11px] text-gray-600" :title="link.link_path">
                {{ link.link_path }}
              </p>
              <!-- 扣留观察中：文件已消失但还没删，说清楚什么时候删 -->
              <p
                v-if="link.pending_delete"
                class="text-[11px] text-amber-400"
                :title="`发现消失 ${shortTime(link.pending_delete.detected_time)}`"
              >
                ⏳ {{ deleteLabel(link.pending_delete) }} ·
                {{ shortTime(link.pending_delete.delete_at) }}
                <span class="text-gray-600">
                  （{{ link.pending_delete.side === 'source' ? '源文件消失' : '媒体库文件消失' }}）
                </span>
              </p>
            </div>
            <button
              class="btn-ghost shrink-0 px-2 py-0.5 text-[11px]"
              title="只删这条记录，不碰文件"
              @click="dropRecord(link)"
            >
              删记录
            </button>
          </div>

          <div class="flex items-start gap-2 border-t border-gray-800 pt-1.5">
            <span
              class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full"
              :class="link.source_exists ? 'bg-emerald-500' : 'bg-red-500'"
              :title="link.source_exists ? '源文件存在' : '源文件已丢失'"
            />
            <div class="min-w-0 flex-1">
              <p class="text-[11px] text-gray-500">源文件</p>
              <p class="truncate text-[11px] text-gray-600" :title="link.source_path">
                {{ link.source_path }}
              </p>
            </div>
            <span v-if="link.inode" class="shrink-0 font-mono text-[11px] text-gray-700">
              inode {{ link.inode }}
            </span>
          </div>
        </div>
      </div>
    </div>

    <!-- 分页 -->
    <div v-if="total" class="flex flex-wrap items-center justify-center gap-2">
      <span class="mr-auto text-xs text-gray-500">
        第 {{ rangeStart }}–{{ rangeEnd }} 条 / 共 {{ total }} 条
      </span>

      <button class="btn-ghost px-3 py-1.5 text-xs" :disabled="page <= 1" @click="go(1)">
        首页
      </button>
      <button class="btn-ghost px-3 py-1.5 text-xs" :disabled="page <= 1" @click="go(page - 1)">
        上一页
      </button>
      <span class="text-xs text-gray-500">{{ page }} / {{ pages }}</span>
      <button
        class="btn-ghost px-3 py-1.5 text-xs"
        :disabled="page >= pages"
        @click="go(page + 1)"
      >
        下一页
      </button>
      <button
        class="btn-ghost px-3 py-1.5 text-xs"
        :disabled="page >= pages"
        @click="go(pages)"
      >
        末页
      </button>

      <select
        class="input ml-2 w-auto py-1.5 text-xs"
        :value="size"
        @change="changeSize($event.target.value)"
      >
        <option v-for="n in SIZE_OPTIONS" :key="n" :value="n">{{ n }} 条/页</option>
      </select>
    </div>

    <!-- 手工登记 -->
    <div
      v-if="registerOpen"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      @click.self="registerOpen = false"
    >
      <div class="card w-full max-w-md space-y-3">
        <p class="text-sm font-medium text-gray-200">手工登记硬链接</p>
        <p class="text-[11px] text-gray-500">
          按源文件的 inode 在媒体库里扫描硬链接。源文件必须存在，且与刮削产物在同一挂载卷。
        </p>

        <div>
          <label class="label">番号</label>
          <input v-model="registerDraft.code" class="input" placeholder="ABC-123" />
        </div>
        <div>
          <label class="label">源文件路径</label>
          <input
            v-model="registerDraft.source_path"
            class="input font-mono text-xs"
            placeholder="/downloads/ABC-123/ABC-123.mp4"
          />
        </div>
        <div>
          <label class="label">硬链接路径（可选）</label>
          <input
            v-model="registerDraft.link_path"
            class="input font-mono text-xs"
            placeholder="留空则自动扫描媒体库"
          />
        </div>

        <div class="flex justify-end gap-2 pt-1">
          <button class="btn-ghost px-3 py-1.5 text-xs" @click="registerOpen = false">
            取消
          </button>
          <button
            class="btn-primary px-3 py-1.5 text-xs"
            :disabled="registering"
            @click="submitRegister"
          >
            {{ registering ? '登记中…' : '登记' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 删除确认：先演练，看清范围再动手 -->
    <div
      v-if="confirming"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      @click.self="confirming = null"
    >
      <div class="card max-h-[80vh] w-full max-w-lg space-y-3 overflow-y-auto">
        <p class="text-sm font-medium text-gray-200">
          联动删除 <span class="font-mono text-brand">{{ confirming.code }}</span>
        </p>

        <p v-if="previewing" class="text-xs text-gray-500">正在演练，查询会删除的内容…</p>

        <template v-else-if="preview">
          <p class="text-xs text-gray-400">以下内容将被删除：</p>

          <div class="space-y-2 text-[11px]">
            <div>
              <p class="text-gray-500">
                种子（{{ (preview.torrents_deleted || []).length }} 个，仅删任务不删文件）
              </p>
              <p
                v-for="h in preview.torrents_deleted"
                :key="h"
                class="truncate font-mono text-gray-400"
              >
                {{ h }}
              </p>
              <p v-if="!(preview.torrents_deleted || []).length" class="text-gray-600">无</p>
            </div>

            <div>
              <p class="text-gray-500">
                源文件（{{ (preview.files_deleted || []).length }} 个）
              </p>
              <p
                v-for="p in preview.files_deleted"
                :key="p"
                class="truncate text-red-400"
                :title="p"
              >
                {{ p }}
              </p>
              <p v-if="!(preview.files_deleted || []).length" class="text-gray-600">无</p>
            </div>

            <div>
              <p class="text-gray-500">
                硬链接（{{ (preview.links_deleted || []).length }} 个）
              </p>
              <p
                v-for="p in preview.links_deleted"
                :key="p"
                class="truncate text-red-400"
                :title="p"
              >
                {{ p }}
              </p>
            </div>

            <div v-if="(preview.sidecars_deleted || []).length">
              <p class="text-gray-500">
                刮削附属（{{ preview.sidecars_deleted.length }} 个，配置/图片/字幕）
              </p>
              <p
                v-for="p in preview.sidecars_deleted"
                :key="p"
                class="truncate text-red-400"
                :title="p"
              >
                {{ p }}
              </p>
            </div>

            <div v-if="(preview.dirs_deleted || []).length">
              <p class="text-gray-500">
                空目录（{{ preview.dirs_deleted.length }} 个）
              </p>
              <p
                v-for="p in preview.dirs_deleted"
                :key="p"
                class="truncate text-red-400"
                :title="p"
              >
                {{ p }}
              </p>
            </div>

            <p v-if="(preview.errors || []).length" class="text-amber-400">
              {{ preview.errors.join('; ') }}
            </p>
          </div>

          <p class="rounded-lg bg-red-950/40 px-3 py-2 text-[11px] text-red-300">
            文件删除不可撤销。下载历史也会一并清掉，之后订阅可能重新下载该番号。
          </p>
        </template>

        <div class="flex justify-end gap-2 pt-1">
          <button class="btn-ghost px-3 py-1.5 text-xs" @click="confirming = null">取消</button>
          <button
            class="rounded-lg bg-red-600 px-3 py-1.5 text-xs text-white transition-colors hover:bg-red-500 disabled:opacity-50"
            :disabled="deleting || previewing || !preview"
            @click="confirmDelete"
          >
            {{ deleting ? '删除中…' : deleteEnabled ? '确认删除' : '执行（仅演练）' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 批量操作确认。选中多条时不逐条演练 —— 几十次演练要几十轮下载器
         往返，把要删什么说清楚比逐条列出来更实际 -->
    <div
      v-if="batchConfirm"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      @click.self="batchConfirm = ''"
    >
      <div class="card w-full max-w-md space-y-3">
        <p class="text-sm font-medium text-gray-200">
          {{ batchConfirm === 'delete' ? '批量联动删除' : '批量删除记录' }}
          <span class="font-mono text-brand">{{ pickedCount }}</span> 条
        </p>

        <template v-if="batchConfirm === 'delete'">
          <p class="text-[11px] text-gray-400">
            对选中的每条关联执行：删种 → 删源文件 → 删媒体库里的硬链接 →
            清理刮削附属与空目录 → 清记录。Emby 里对应的影片会消失。
          </p>
          <p class="rounded-lg bg-red-950/40 px-3 py-2 text-[11px] text-red-300">
            文件删除不可撤销。下载历史也会一并清掉，之后订阅可能重新下载这些番号。
          </p>
          <p v-if="!deleteEnabled" class="text-[11px] text-amber-300">
            联动删除未启用，本次只会演练，不会真的删除任何文件。
          </p>
        </template>

        <template v-else>
          <p class="text-[11px] text-gray-400">
            只删掉库里的关联记录，磁盘上的文件与下载器里的种子都不动。
            用于清理已经手工处理干净的孤儿记录。
          </p>
        </template>

        <div class="flex justify-end gap-2 pt-1">
          <button
            class="btn-ghost px-3 py-1.5 text-xs"
            :disabled="batching"
            @click="batchConfirm = ''"
          >
            取消
          </button>
          <button
            v-if="batchConfirm === 'delete'"
            class="rounded-lg bg-red-600 px-3 py-1.5 text-xs text-white transition-colors hover:bg-red-500 disabled:opacity-50"
            :disabled="batching"
            @click="runBatchDelete"
          >
            {{ batching ? '删除中…' : deleteEnabled ? `确认删除 ${pickedCount} 条` : '执行（仅演练）' }}
          </button>
          <button
            v-else
            class="btn-primary px-3 py-1.5 text-xs"
            :disabled="batching"
            @click="runBatchDropRecords"
          >
            {{ batching ? '处理中…' : `确认删除 ${pickedCount} 条记录` }}
          </button>
        </div>
      </div>
    </div>

    <!-- 记录重建的配对结果 -->
    <div
      v-if="recoverPreview"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      @click.self="recoverPreview = null"
    >
      <div class="card max-h-[80vh] w-full max-w-lg space-y-3 overflow-y-auto">
        <p class="text-sm font-medium text-gray-200">重建缺失的关联记录</p>
        <p class="text-[11px] text-gray-500">
          按番号从下载历史里找回源文件路径。补的是「纳入管理」配不上的那批 ——
          那个要求源文件当前还在下载器里，这个源文件已删也能重建。
        </p>

        <div class="flex flex-wrap gap-2 text-[11px]">
          <span class="badge bg-gray-800 text-gray-400">
            未登记 {{ recoverPreview.total }}
          </span>
          <span class="badge bg-emerald-950/60 text-emerald-300">
            可重建 {{ (recoverPreview.recovered || []).length }}
          </span>
          <span class="badge bg-gray-800 text-gray-500">
            配不上 {{ (recoverPreview.unmatched || []).length }}
          </span>
        </div>

        <div v-if="(recoverPreview.recovered || []).length" class="space-y-1">
          <p class="text-[11px] text-gray-400">将重建（显示前 10 条）</p>
          <div
            v-for="row in recoverPreview.recovered.slice(0, 10)"
            :key="row.link_path"
            class="rounded bg-gray-900/60 px-2 py-1.5"
          >
            <p class="font-mono text-[11px] text-brand">
              {{ row.code }}
              <span v-if="!row.source_exists" class="text-red-400">（源文件已删）</span>
            </p>
            <p class="truncate text-[11px] text-gray-500" :title="row.link_path">
              {{ fileName(row.link_path) }}
            </p>
            <p class="truncate text-[11px] text-gray-600" :title="row.source_path">
              → {{ row.source_path }}
            </p>
          </div>
          <p
            v-if="recoverPreview.recovered.length > 10"
            class="text-[11px] text-gray-600"
          >
            还有 {{ recoverPreview.recovered.length - 10 }} 条…
          </p>
        </div>

        <p
          v-if="(recoverPreview.errors || []).length"
          class="rounded-lg bg-red-950/40 px-3 py-2 text-[11px] text-red-300"
        >
          {{ recoverPreview.errors.join('; ') }}
        </p>

        <div class="flex justify-end gap-2 pt-1">
          <button class="btn-ghost px-3 py-1.5 text-xs" @click="recoverPreview = null">
            取消
          </button>
          <button
            class="btn-primary px-3 py-1.5 text-xs"
            :disabled="recovering || !(recoverPreview.recovered || []).length"
            @click="confirmRecover"
          >
            {{ recovering ? '重建中…' : `确认重建 ${(recoverPreview.recovered || []).length} 条` }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
