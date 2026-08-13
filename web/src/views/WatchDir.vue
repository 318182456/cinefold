<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  adoptScrapeDir, backfillWatchDirTorrents, cancelWatchDirHold, createWatchDir,
  deleteWatchDir, getWatchDirProgress, listWatchDirHolds, listWatchDirs,
  syncAllWatchDirs, syncWatchDir, updateWatchDir,
} from '@/api'
import { useToast } from '@/composables/useToast'
import LoadingBlock from '@/components/LoadingBlock.vue'

const toast = useToast()

const items = ref([])
const loading = ref(false)
const libraryPath = ref('')
const libraryExists = ref(false)
const deleteEnabled = ref(false)
const watching = ref(false)
const watchingStarting = ref(false)
// 定时对账间隔（分钟）与自动同步总开关，供提示文案使用
const syncInterval = ref(30)
const autoSync = ref(true)

// 扣留中的删除
const holds = ref([])
const graceSeconds = ref(0)

// 新增 / 编辑
const editorOpen = ref(false)
const editing = ref(null)
const draft = ref(blankDraft())
const saving = ref(false)

// 同步结果预览。真同步前先看演练结果
const syncResult = ref(null)
const syncing = ref(false)
const confirmingRule = ref(null)

// 刮削输出目录既存文件纳管的演练结果
const adoptOpen = ref(false)
const adoptResult = ref(null)
const adopting = ref(false)

// 同步进度。真同步走后台线程，靠轮询这个接口看它跑到哪了 ——
// 每个新文件都要等写入稳定（最多 6 秒），没有进度就完全是黑盒
const progress = ref([])
let progressTimer = null
let watchTimer = null

const runningProgress = computed(() => progress.value.filter((p) => p.running))

function progressPercent(item) {
  if (!item.total) return 0
  return Math.min(100, Math.round((item.done / item.total) * 100))
}

const PHASE_LABELS = {
  scanning: '扫描源目录',
  claiming: '核对已有链接',
  linking: '建立硬链接',
  checking: '核对删除',
  done: '已完成',
}

// 直通模式不建硬链接，只登记 —— 沿用通用文案会跟规则卡片上的
// 「同源目录（不建链接）」对不上
const PASSTHROUGH_PHASE_LABELS = {
  ...PHASE_LABELS,
  linking: '登记影片',
}

function phaseLabel(item) {
  const labels = item.passthrough ? PASSTHROUGH_PHASE_LABELS : PHASE_LABELS
  return labels[item.phase] || item.phase || ''
}

async function loadProgress() {
  try {
    const data = await getWatchDirProgress()
    progress.value = data.items || []
    // 跑完了就停轮询，并刷新列表拿到最终结果
    if (!data.running && progressTimer) {
      stopProgressPoll()
      await Promise.all([load(), loadHolds()])
    }
  } catch {
    // 进度拿不到不影响别的，下一轮再试
  }
}

function startProgressPoll() {
  if (progressTimer) return
  loadProgress()
  progressTimer = setInterval(loadProgress, 2000)
}

function stopProgressPoll() {
  if (progressTimer) {
    clearInterval(progressTimer)
    progressTimer = null
  }
}

// 监听建立中的轮询。只刷状态字段，不走 load() —— 那个会翻 loading，
// 列表每 5 秒闪一次加载态
async function pollWatchState() {
  try {
    const data = await listWatchDirs()
    watching.value = !!data.watching
    watchingStarting.value = !!data.watching_starting
    if (!watchingStarting.value) stopWatchPoll()
  } catch {
    // 查不到就下一轮再说，不打断页面
  }
}

function startWatchPoll() {
  if (watchTimer) return
  watchTimer = setInterval(pollWatchState, 5000)
}

function stopWatchPoll() {
  if (watchTimer) {
    clearInterval(watchTimer)
    watchTimer = null
  }
}

function blankDraft() {
  return {
    source_dir: '',
    // 目标目录绝对路径，与媒体库根目录无关
    target_dir: '',
    // 旧字段：相对媒体库根目录的子目录。target_dir 为空时才生效
    target_subdir: '',
    name: '',
    enabled: true,
    recursive: true,
    reverse_delete: false,
    code_prefix: '',
    // 直通模式：不建硬链接，Emby 直接扫源目录
    passthrough: false,
  }
}

// 目标目录的实际落点，与后端 _resolve_target 的优先级保持一致
const targetPreview = computed(() => {
  // 直通模式没有目标目录，源目录自己就是
  if (draft.value.passthrough) return (draft.value.source_dir || '').trim()
  const dir = (draft.value.target_dir || '').trim()
  if (dir) return dir
  if (!libraryPath.value) return ''
  const sub = (draft.value.target_subdir || '').trim()
  return sub ? `${libraryPath.value}/${sub}` : libraryPath.value
})

const graceLabel = computed(() => {
  const s = graceSeconds.value
  if (!s) return '关闭（发现即删）'
  if (s % 3600 === 0) return `${s / 3600} 小时`
  if (s % 60 === 0) return `${s / 60} 分钟`
  return `${s} 秒`
})

function fileName(path) {
  return (path || '').split(/[\\/]/).pop() || path
}

// 刮削输出目录才有 registered_count：目录里实际存在多少影片、其中多少条
// 登记过关联。差额是刮削工具建好但从没走 webhook 的既存文件
function unregisteredCount(item) {
  if (item.registered_count === undefined) return 0
  return Math.max(0, (item.file_count || 0) - item.registered_count)
}

function waitLabel(item) {
  const left = Math.max(0, (item.grace_seconds || 0) - (item.waited_seconds || 0))
  if (left <= 0) return '下轮对账将删除'
  if (left >= 3600) return `还有 ${Math.ceil(left / 3600)} 小时`
  if (left >= 60) return `还有 ${Math.ceil(left / 60)} 分钟`
  return `还有 ${left} 秒`
}

async function load() {
  loading.value = true
  try {
    const data = await listWatchDirs()
    items.value = data.items || []
    libraryPath.value = data.library_path || ''
    libraryExists.value = !!data.library_exists
    deleteEnabled.value = !!data.delete_enabled
    watching.value = !!data.watching
    watchingStarting.value = !!data.watching_starting
    syncInterval.value = data.sync_interval || 30
    autoSync.value = data.auto_sync !== false

    // 建监听要几分钟，轮询着等它好，省得用户自己刷页面
    if (watchingStarting.value) startWatchPoll()
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

async function loadHolds() {
  try {
    const data = await listWatchDirHolds()
    holds.value = data.items || []
    graceSeconds.value = data.grace_seconds || 0
  } catch {
    // 扣留列表拿不到不影响主列表
  }
}

// ---------------------------------------------------------------- 编辑
function openCreate() {
  editing.value = null
  draft.value = blankDraft()
  editorOpen.value = true
}

function openEdit(item) {
  editing.value = item
  draft.value = {
    source_dir: item.source_dir,
    target_dir: item.target_dir || '',
    target_subdir: item.target_subdir,
    name: item.name,
    enabled: item.enabled,
    recursive: item.recursive,
    reverse_delete: item.reverse_delete,
    code_prefix: item.code_prefix,
    passthrough: !!item.passthrough,
  }
  editorOpen.value = true
}

async function save() {
  const payload = { ...draft.value }
  payload.source_dir = (payload.source_dir || '').trim()
  if (!payload.source_dir) {
    toast.error('源目录不能为空')
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      await updateWatchDir(editing.value.id, payload)
      toast.success('已更新')
    } else {
      await createWatchDir(payload)
      toast.success('已添加监控目录')
    }
    editorOpen.value = false
    await load()
  } catch (err) {
    toast.error(err.message)
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(item) {
  try {
    await updateWatchDir(item.id, { enabled: !item.enabled })
    await load()
  } catch (err) {
    toast.error(err.message)
  }
}

async function remove(item) {
  try {
    await deleteWatchDir(item.id)
    toast.success('已删除规则，已建立的硬链接保留')
    await load()
  } catch (err) {
    toast.error(err.message)
  }
}

// ---------------------------------------------------------------- 同步
// 先演练：让用户看到确切范围再决定是否真同步
async function preview(item) {
  confirmingRule.value = item
  syncResult.value = null
  syncing.value = true
  try {
    syncResult.value = await syncWatchDir(item.id, true)
  } catch (err) {
    toast.error(err.message)
    confirmingRule.value = null
  } finally {
    syncing.value = false
  }
}

// 真同步走后台：每个新文件要等写入稳定（最多 6 秒），同步等待必然超时
async function runSync() {
  const item = confirmingRule.value
  syncing.value = true
  try {
    await syncWatchDir(item.id, false, true)
    toast.success('已在后台开始同步')
    confirmingRule.value = null
    startProgressPoll()
  } catch (err) {
    toast.error(err.message)
  } finally {
    syncing.value = false
  }
}

async function syncEverything() {
  syncing.value = true
  try {
    await syncAllWatchDirs(false, true)
    toast.success('已在后台开始全量对账')
    startProgressPoll()
  } catch (err) {
    toast.error(err.message)
  } finally {
    syncing.value = false
  }
}

// 建链接时下载器里可能还没有种子，事后补一次
async function backfill() {
  syncing.value = true
  try {
    const data = await backfillWatchDirTorrents()
    const n = data.added || 0
    toast.success(n ? `补登记 ${n} 个种子` : '没有需要补登记的种子')
  } catch (err) {
    toast.error(err.message)
  } finally {
    syncing.value = false
  }
}

// 刮削输出目录的既存文件纳管。先演练看配对结果，再执行 ——
// 登记的是反向删除的依据，配错等于把删除权指向错误的源文件
async function previewAdopt() {
  adoptResult.value = null
  adoptOpen.value = true
  adopting.value = true
  try {
    adoptResult.value = await adoptScrapeDir(true)
  } catch (err) {
    toast.error(err.message)
    adoptOpen.value = false
  } finally {
    adopting.value = false
  }
}

async function runAdopt() {
  adopting.value = true
  try {
    const data = await adoptScrapeDir(false)
    toast.success(`已纳入管理 ${(data.adopted || []).length} 个影片`)
    adoptOpen.value = false
    await load()
  } catch (err) {
    toast.error(err.message)
  } finally {
    adopting.value = false
  }
}

async function cancelHold(hold) {
  try {
    await cancelWatchDirHold(hold.link_path)
    toast.success('已撤销扣留')
    await loadHolds()
  } catch (err) {
    toast.error(err.message)
  }
}

onMounted(async () => {
  load()
  loadHolds()
  // 进来时可能正好有定时对账在跑（或上次离开页面时后台还没跑完），
  // 先查一次，有在跑的就接着轮询
  await loadProgress()
  if (progress.value.some((p) => p.running)) startProgressPoll()
})

onUnmounted(() => {
  stopProgressPoll()
  stopWatchPoll()
})
</script>

<template>
  <div class="space-y-4">
    <!-- 概览 -->
    <div class="grid gap-3 sm:grid-cols-4">
      <div class="card">
        <p class="text-xs text-gray-500">监控目录</p>
        <p class="mt-1 text-xl font-semibold text-gray-200">{{ items.length }}</p>
      </div>
      <div class="card">
        <p class="text-xs text-gray-500">实时监听</p>
        <p
          class="mt-1 text-xl font-semibold"
          :class="watching ? 'text-emerald-400' : watchingStarting ? 'text-sky-400' : 'text-gray-500'"
        >
          {{ watching ? '运行中' : watchingStarting ? '建立中' : '未启用' }}
        </p>
      </div>
      <div class="card">
        <p class="text-xs text-gray-500">删除宽限期</p>
        <p class="mt-1 text-sm font-semibold text-gray-200">{{ graceLabel }}</p>
      </div>
      <div class="card">
        <p class="text-xs text-gray-500">扣留观察中</p>
        <p
          class="mt-1 text-xl font-semibold"
          :class="holds.length ? 'text-amber-400' : 'text-gray-200'"
        >
          {{ holds.length }}
        </p>
      </div>
    </div>

    <!-- 环境提示。媒体库根目录不再是必需的 —— 每条规则可自带目标目录，
         所以这里只提示「没配根目录时新建规则必须填目标目录」 -->
    <p v-if="!libraryPath" class="rounded-lg bg-gray-800/60 px-3 py-2 text-xs text-gray-400">
      未配置媒体库根目录（MEDIALINK_LIBRARY_PATH）。不影响使用，但每条规则都必须自己填目标目录。
    </p>
    <p
      v-else-if="!libraryExists"
      class="rounded-lg bg-amber-950/40 px-3 py-2 text-xs text-amber-300"
    >
      媒体库根目录不存在或不可读：{{ libraryPath }}。填了目标目录的规则不受影响。
    </p>
    <p v-if="!autoSync" class="rounded-lg bg-amber-950/40 px-3 py-2 text-xs text-amber-300">
      自动同步已关闭（WATCHDIR_AUTO_SYNC=false）。定时对账与实时监听都不会运行，
      只有手动点「全部对账」或单条规则的「同步」才会执行。
    </p>
    <p
      v-else-if="watchingStarting"
      class="rounded-lg bg-sky-950/40 px-3 py-2 text-xs text-sky-300"
    >
      实时监听正在后台建立。inotify 不支持递归，需要逐个子目录注册，
      目录多或放在 NAS 上时可能要几分钟。这期间同步依赖定时对账（每
      {{ syncInterval }} 分钟），不影响使用。
    </p>
    <p
      v-else-if="!watching"
      class="rounded-lg bg-gray-800/60 px-3 py-2 text-xs text-gray-400"
    >
      实时监听未运行，同步依赖定时对账（每 {{ syncInterval }} 分钟）。可能是未安装
      watchdog、没有启用的规则，或源目录不可读。
      NAS / Docker 绑定挂载上 inotify 事件常收不到，属正常现象 ——
      需要立刻生效请点「全部对账」。
    </p>

    <!-- 同步进度。后台跑的时候这里实时显示到哪一步了 -->
    <div v-if="progress.length" class="card space-y-2">
      <div class="flex items-center gap-2">
        <p class="text-xs font-medium text-gray-300">同步进度</p>
        <span
          v-if="runningProgress.length"
          class="badge bg-emerald-950/60 text-emerald-400"
        >
          {{ runningProgress.length }} 条进行中
        </span>
      </div>

      <div
        v-for="p in progress"
        :key="p.rule_id"
        class="space-y-1 rounded-lg bg-gray-900/60 px-3 py-2"
      >
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-xs text-gray-300">{{ p.name }}</span>
          <span
            class="badge"
            :class="p.running ? 'bg-emerald-950/60 text-emerald-400' : 'bg-gray-800 text-gray-500'"
          >
            {{ p.running ? phaseLabel(p) : '已完成' }}
          </span>
          <span v-if="p.total" class="text-[11px] text-gray-500">
            {{ p.done }} / {{ p.total }}
          </span>
          <span class="ml-auto text-[11px] text-gray-600">
            {{ p.finished_at || p.started_at }}
          </span>
        </div>

        <!-- 进度条只在建链接阶段有意义，其余阶段没有可数的分母 -->
        <div v-if="p.running && p.total" class="h-1 overflow-hidden rounded bg-gray-800">
          <div
            class="h-full bg-emerald-500 transition-all duration-300"
            :style="{ width: `${progressPercent(p)}%` }"
          />
        </div>

        <p v-if="p.current" class="truncate text-[11px] text-gray-500" :title="p.current">
          正在处理：{{ p.current }}
        </p>
        <p v-if="p.message" class="truncate text-[11px] text-gray-600">
          {{ p.message }}
        </p>
      </div>
    </div>

    <!-- 工具条 -->
    <div class="flex flex-wrap items-center gap-2">
      <button class="btn-ghost px-3 py-1.5 text-xs" :disabled="syncing" @click="syncEverything">
        全部对账
      </button>
      <button
        class="btn-ghost px-3 py-1.5 text-xs"
        :disabled="syncing"
        title="给还没登记种子的关联补查下载器。建链接时种子可能还没下载完"
        @click="backfill"
      >
        补登记种子
      </button>
      <button class="btn-primary ml-auto px-3 py-1.5 text-xs" @click="openCreate">
        添加监控目录
      </button>
    </div>

    <LoadingBlock v-if="loading" :rows="4" />

    <p v-else-if="!items.length" class="card text-center text-sm text-gray-500">
      还没有监控目录。添加后，目录里的文件增删会自动同步为媒体库的硬链接。
    </p>

    <!-- 规则列表 -->
    <div v-else class="space-y-3">
      <div v-for="item in items" :key="item.id" class="card space-y-2">
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-sm font-medium text-gray-200">
            {{ item.name || fileName(item.source_dir) }}
          </span>
          <span v-if="item.protected" class="badge bg-sky-950/60 text-sky-400">
            受保护
          </span>
          <span
            v-else
            class="badge"
            :class="item.enabled ? 'bg-emerald-950/60 text-emerald-400' : 'bg-gray-800 text-gray-500'"
          >
            {{ item.enabled ? '启用' : '停用' }}
          </span>
          <span v-if="item.passthrough" class="badge bg-sky-950/60 text-sky-400">
            直通
          </span>
          <span v-if="item.reverse_delete" class="badge bg-red-950/60 text-red-400">
            反向删除
          </span>
          <span
            v-if="!item.recursive && !item.protected"
            class="badge bg-gray-800 text-gray-400"
          >不含子目录</span>
          <span v-if="item.code_prefix" class="badge bg-gray-800 text-gray-400">
            前缀 {{ item.code_prefix }}
          </span>
          <span v-if="item.file_count" class="text-[11px] text-gray-600">
            {{ item.file_count }} 个文件
          </span>
          <!-- 刮削输出目录里可能有刮削工具建好但没走 webhook 的既存文件，
               它们不在删除联动的管辖范围内，得明确标出来 -->
          <span
            v-if="unregisteredCount(item)"
            class="badge bg-amber-950/60 text-amber-400"
            title="这些文件没有登记关联：在 Emby 里删掉不会连带删除源文件与种子"
          >
            {{ unregisteredCount(item) }} 个未登记
          </span>
          <span v-if="item.last_scan_time" class="text-[11px] text-gray-600">
            {{ item.last_scan_time }}
          </span>

          <!-- 刮削输出目录是占位项，由 webhook 逐条登记，没有同步/编辑动作。
               唯一的例外是纳管既存文件 —— webhook 只管它上报过的那些 -->
          <div v-if="item.protected" class="ml-auto flex items-center gap-2">
            <button
              v-if="unregisteredCount(item)"
              class="btn-ghost px-2 py-0.5 text-[11px] text-amber-400"
              :disabled="adopting"
              title="按 inode 把这些文件配回下载器里的源文件与种子，纳入删除联动"
              @click="previewAdopt"
            >
              纳入管理
            </button>
            <span class="text-[11px] text-gray-600">
              在「设置 → 媒体联动」中修改
            </span>
          </div>
          <div v-else class="ml-auto flex items-center gap-1">
            <button class="btn-ghost px-2 py-0.5 text-[11px]" @click="preview(item)">
              同步
            </button>
            <button class="btn-ghost px-2 py-0.5 text-[11px]" @click="toggleEnabled(item)">
              {{ item.enabled ? '停用' : '启用' }}
            </button>
            <button class="btn-ghost px-2 py-0.5 text-[11px]" @click="openEdit(item)">
              编辑
            </button>
            <button
              class="btn-ghost px-2 py-0.5 text-[11px] text-red-400 hover:bg-red-950/40"
              @click="remove(item)"
            >
              删除
            </button>
          </div>
        </div>

        <div class="space-y-1 rounded-lg bg-gray-900/60 px-3 py-2">
          <!-- 刮削输出目录没有固定源目录：源文件位置由 webhook 逐条上报，
               一个番号一个位置，写死一个路径是错的 -->
          <div v-if="!item.source_dir" class="flex items-start gap-2">
            <span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-gray-600" />
            <div class="min-w-0 flex-1">
              <p class="text-[11px] text-gray-500">源文件</p>
              <p class="truncate text-xs text-gray-500">
                由刮削工具通过 webhook 逐条登记，位置随番号而定
              </p>
            </div>
          </div>
          <div v-else class="flex items-start gap-2">
            <span
              class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full"
              :class="item.source_exists ? 'bg-emerald-500' : 'bg-red-500'"
              :title="item.source_exists ? '源目录可读' : '源目录不存在'"
            />
            <div class="min-w-0 flex-1">
              <p class="text-[11px] text-gray-500">源目录</p>
              <p class="truncate text-xs text-gray-300" :title="item.source_dir">
                {{ item.source_dir }}
              </p>
            </div>
          </div>
          <div class="flex items-start gap-2 border-t border-gray-800 pt-1.5">
            <span
              class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full"
              :class="item.target_exists ? 'bg-emerald-500' : 'bg-gray-600'"
              :title="item.target_exists ? '目标目录已存在' : '目标目录尚未创建，建链接时自动建'"
            />
            <div class="min-w-0 flex-1">
              <p class="text-[11px] text-gray-500">
                {{ item.protected ? '输出目录' : (item.passthrough ? '媒体库目录' : '目标目录') }}
              </p>
              <p
                v-if="item.passthrough"
                class="truncate text-[11px] text-gray-600"
                title="直通模式：源目录即媒体库目录，不建硬链接"
              >
                同源目录（不建链接）
              </p>
              <p
                v-else
                class="truncate text-[11px] text-gray-600"
                :title="item.resolved_target"
              >
                {{ item.resolved_target || '未配置' }}
              </p>
            </div>
          </div>
        </div>

        <!-- 未登记的既存文件不在删除联动范围内，也没有自动补全的途径 ——
             刮削输出目录不是监控规则，「全部对账」不会扫它 -->
        <p v-if="unregisteredCount(item)" class="text-[11px] text-amber-400">
          有 {{ unregisteredCount(item) }} 个影片没有登记关联（多为启用 cinefold
          之前刮削的）。它们在 Emby 里删掉不会连带删除源文件与种子 ——
          点「纳入管理」按 inode 配回下载器里的源文件。
        </p>

        <p v-if="item.last_error" class="text-[11px] text-amber-400">
          {{ item.last_error }}
        </p>
      </div>
    </div>

    <!-- 扣留中的删除 -->
    <div v-if="holds.length" class="card space-y-2">
      <div class="flex items-center gap-2">
        <p class="text-sm font-medium text-gray-200">扣留观察中</p>
        <span class="badge bg-amber-950/60 text-amber-400">{{ holds.length }}</span>
      </div>
      <p class="text-[11px] text-gray-500">
        这些文件已消失，但还在宽限期内。期间若同一份文件在别处出现，会判定为移动而不删除。
      </p>
      <div
        v-for="hold in holds"
        :key="hold.link_path"
        class="flex items-start gap-2 rounded-lg bg-gray-900/60 px-3 py-2"
      >
        <div class="min-w-0 flex-1">
          <p class="truncate text-xs text-gray-300" :title="hold.link_path">
            {{ fileName(hold.link_path) }}
          </p>
          <p class="truncate text-[11px] text-gray-600" :title="hold.source_path">
            {{ hold.side === 'source' ? '源文件消失' : '媒体库文件消失' }} ·
            {{ hold.source_path }}
          </p>
        </div>
        <span class="shrink-0 text-[11px] text-amber-400">{{ waitLabel(hold) }}</span>
        <button class="btn-ghost shrink-0 px-2 py-0.5 text-[11px]" @click="cancelHold(hold)">
          撤销
        </button>
      </div>
    </div>

    <!-- 编辑弹窗 -->
    <div
      v-if="editorOpen"
      class="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4"
      @click.self="editorOpen = false"
    >
      <div class="card w-full max-w-lg space-y-3">
        <p class="text-sm font-medium text-gray-200">
          {{ editing ? '编辑监控目录' : '添加监控目录' }}
        </p>

        <div class="space-y-1">
          <label class="text-xs text-gray-500">源目录（绝对路径）</label>
          <input v-model="draft.source_dir" class="input" placeholder="/downloads/短视频" />
          <p v-if="!draft.passthrough" class="text-[11px] text-gray-600">
            必须与目标目录在同一文件系统，否则无法建硬链接。
          </p>
          <p v-else class="text-[11px] text-gray-600">
            把这个目录直接添加为 Emby 的媒体库。
          </p>
        </div>

        <!-- 直通模式决定下面是否还需要目标目录，所以放在源目录紧后面 -->
        <label class="flex items-start gap-2 text-xs text-gray-400">
          <input v-model="draft.passthrough" type="checkbox" class="mt-0.5 accent-brand" />
          <span>
            直通模式：不建硬链接，Emby 直接扫源目录
            <span class="block text-[11px] text-gray-500">
              适合不需要刮削、不需要整理的内容（短视频等）。只登记关联与种子，
              让「Emby 里删片 → 删源文件 + 删种」这条联动生效。
            </span>
          </span>
        </label>

        <template v-if="!draft.passthrough">
          <div class="space-y-1">
            <label class="text-xs text-gray-500">目标目录（绝对路径）</label>
            <input
              v-model="draft.target_dir"
              class="input"
              placeholder="/volume3/h_video/短视频"
            />
            <p class="text-[11px] text-gray-600">
              想放哪就填哪，与设置里的媒体库根目录无关 —— 短视频、电影各有独立媒体库时用这个。
            </p>
          </div>

          <!-- 旧配置方式，只在还没填目标目录时露出来，避免两个字段同时可见让人困惑 -->
          <div v-if="!draft.target_dir.trim()" class="space-y-1">
            <label class="text-xs text-gray-500">或：媒体库子目录（可留空）</label>
            <input v-model="draft.target_subdir" class="input" placeholder="短视频" />
            <p class="text-[11px] text-gray-600">
              留空目标目录时才生效，拼在媒体库根目录
              {{ libraryPath || '（未配置）' }} 之后。
            </p>
          </div>

          <p
            v-if="targetPreview"
            class="rounded-lg bg-gray-900/60 px-3 py-2 text-[11px] text-gray-400"
          >
            链接将建到 <span class="text-gray-300">{{ targetPreview }}</span>，
            源目录内的层级结构原样保留。
          </p>
          <p v-else class="rounded-lg bg-amber-950/40 px-3 py-2 text-[11px] text-amber-300">
            请填写目标目录 —— 媒体库根目录也没配置，无处建立链接。
          </p>
        </template>

        <p
          v-else
          class="rounded-lg bg-amber-950/40 px-3 py-2 text-[11px] text-amber-300"
        >
          直通模式没有硬链接兜底：Emby 里删掉就是直接删源文件，删了找不回来。
          建议先不开反向删除，用「演练」确认命中的文件和种子都对，再放开。
        </p>

        <div class="grid gap-3 sm:grid-cols-2">
          <div class="space-y-1">
            <label class="text-xs text-gray-500">名称（可留空）</label>
            <input v-model="draft.name" class="input" placeholder="短视频" />
          </div>
          <div class="space-y-1">
            <label class="text-xs text-gray-500">code 前缀（可留空）</label>
            <input v-model="draft.code_prefix" class="input" placeholder="SV" />
          </div>
        </div>

        <label class="flex items-center gap-2 text-xs text-gray-400">
          <input v-model="draft.enabled" type="checkbox" class="accent-brand" />
          启用
        </label>
        <label class="flex items-center gap-2 text-xs text-gray-400">
          <input v-model="draft.recursive" type="checkbox" class="accent-brand" />
          包含子目录
        </label>
        <label class="flex items-start gap-2 text-xs text-gray-400">
          <input v-model="draft.reverse_delete" type="checkbox" class="mt-0.5 accent-brand" />
          <span>
            反向删除：媒体库里删掉影片时，连带删除源文件与种子
            <span class="block text-[11px] text-red-400">
              源文件删除不可恢复。建议先只开正向同步，稳定后再放开。
            </span>
          </span>
        </label>

        <p
          v-if="draft.reverse_delete && !deleteEnabled"
          class="rounded-lg bg-amber-950/40 px-3 py-2 text-[11px] text-amber-300"
        >
          全局联动删除未启用（MEDIALINK_DELETE_ENABLED=false），反向删除只会演练。
        </p>

        <div class="flex justify-end gap-2 pt-1">
          <button class="btn-ghost px-3 py-1.5 text-xs" @click="editorOpen = false">取消</button>
          <button
            class="btn-primary px-3 py-1.5 text-xs"
            :disabled="saving"
            @click="save"
          >
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 同步确认弹窗：先看演练结果 -->
    <div
      v-if="confirmingRule"
      class="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4"
      @click.self="confirmingRule = null"
    >
      <div class="card w-full max-w-lg space-y-3">
        <p class="text-sm font-medium text-gray-200">
          同步 {{ confirmingRule.name || fileName(confirmingRule.source_dir) }}
        </p>

        <LoadingBlock v-if="syncing && !syncResult" :rows="3" />

        <template v-else-if="syncResult">
          <div class="space-y-2 text-xs">
            <p class="text-gray-400">
              新建 {{ (syncResult.linked || []).length }} ·
              认领 {{ (syncResult.claimed || []).length }} ·
              删除 {{ (syncResult.unlinked || []).length }} ·
              移动 {{ (syncResult.moved || []).length }} ·
              反向清理 {{ (syncResult.reverse_deleted || []).length }}
            </p>

            <!-- 认领：目标目录里已经有指向同一份数据的硬链接（多为刮削工具建的），
                 只补登记，不会新建文件，也不会产生重复 -->
            <div v-if="(syncResult.claimed || []).length" class="space-y-1">
              <p class="text-[11px] text-sky-400">
                将认领已有硬链接（只登记，不新建文件）
              </p>
              <p
                v-for="c in syncResult.claimed.slice(0, 8)"
                :key="c.link_path"
                class="truncate text-[11px] text-gray-500"
                :title="`${c.source_path} → ${c.link_path}`"
              >
                {{ c.link_path }}
              </p>
              <p v-if="syncResult.claimed.length > 8" class="text-[11px] text-gray-600">
                还有 {{ syncResult.claimed.length - 8 }} 条…
              </p>
            </div>

            <div v-if="(syncResult.linked || []).length" class="space-y-1">
              <p class="text-[11px] text-emerald-400">
                {{ confirmingRule?.passthrough ? '将登记影片（不建链接）' : '将建立硬链接' }}
              </p>
              <p
                v-for="p in syncResult.linked.slice(0, 8)"
                :key="p"
                class="truncate text-[11px] text-gray-500"
                :title="p"
              >
                {{ p }}
              </p>
              <p v-if="syncResult.linked.length > 8" class="text-[11px] text-gray-600">
                还有 {{ syncResult.linked.length - 8 }} 条…
              </p>
            </div>

            <div v-if="(syncResult.unlinked || []).length" class="space-y-1">
              <p class="text-[11px] text-amber-400">将删除硬链接</p>
              <p
                v-for="p in syncResult.unlinked.slice(0, 8)"
                :key="p"
                class="truncate text-[11px] text-gray-500"
                :title="p"
              >
                {{ p }}
              </p>
            </div>

            <div v-if="(syncResult.moved || []).length" class="space-y-1">
              <p class="text-[11px] text-sky-400">判定为移动（不删不建）</p>
              <p
                v-for="m in syncResult.moved.slice(0, 8)"
                :key="m.from"
                class="truncate text-[11px] text-gray-500"
              >
                {{ fileName(m.from) }} → {{ fileName(m.to) }}
              </p>
            </div>

            <div v-if="(syncResult.reverse_deleted || []).length" class="space-y-1">
              <p class="text-[11px] text-red-400">将删除源文件与种子</p>
              <p
                v-for="r in syncResult.reverse_deleted.slice(0, 8)"
                :key="r.link_path"
                class="truncate text-[11px] text-gray-500"
              >
                {{ r.code }} · 种子 {{ (r.torrents || []).length }} ·
                文件 {{ (r.files || []).length }}
              </p>
            </div>

            <div v-if="(syncResult.skipped || []).length" class="space-y-1">
              <p class="text-[11px] text-gray-500">跳过</p>
              <p
                v-for="(s, i) in syncResult.skipped.slice(0, 5)"
                :key="i"
                class="truncate text-[11px] text-gray-600"
                :title="s"
              >
                {{ s }}
              </p>
            </div>

            <p v-if="(syncResult.errors || []).length" class="text-[11px] text-red-400">
              {{ syncResult.errors.slice(0, 3).join('; ') }}
            </p>
          </div>
        </template>

        <div class="flex justify-end gap-2 pt-1">
          <button class="btn-ghost px-3 py-1.5 text-xs" @click="confirmingRule = null">
            取消
          </button>
          <button
            class="btn-primary px-3 py-1.5 text-xs"
            :disabled="syncing"
            @click="runSync"
          >
            {{ syncing ? '同步中…' : '执行同步' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 刮削输出目录纳管确认：先看 inode 配对结果 -->
    <div
      v-if="adoptOpen"
      class="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4"
      @click.self="adoptOpen = false"
    >
      <div class="card w-full max-w-lg space-y-3">
        <p class="text-sm font-medium text-gray-200">纳入管理（刮削输出目录）</p>

        <LoadingBlock v-if="adopting && !adoptResult" :rows="3" />

        <template v-else-if="adoptResult">
          <p class="text-xs text-gray-400">
            未登记 {{ adoptResult.total }} 个 ·
            可纳管 {{ (adoptResult.adopted || []).length }} ·
            配不到源文件 {{ (adoptResult.unmatched || []).length }} ·
            跳过 {{ (adoptResult.skipped || []).length }}
          </p>

          <div v-if="(adoptResult.adopted || []).length" class="space-y-1">
            <p class="text-[11px] text-emerald-400">
              将登记这些关联（按 inode 配对，不动任何文件）
            </p>
            <p
              v-for="a in adoptResult.adopted.slice(0, 8)"
              :key="a.link_path"
              class="truncate text-[11px] text-gray-500"
              :title="`${a.source_path} → ${a.link_path}`"
            >
              {{ a.code }} · 种子 {{ (a.torrents || []).length }} ·
              {{ fileName(a.link_path) }}
            </p>
            <p v-if="adoptResult.adopted.length > 8" class="text-[11px] text-gray-600">
              还有 {{ adoptResult.adopted.length - 8 }} 条…
            </p>
          </div>

          <!-- 配不到源文件的多是种子已删、或源文件被移走后失去硬链接关系。
               这些没法自动纳管，说清原因免得用户以为按钮没生效 -->
          <div v-if="(adoptResult.unmatched || []).length" class="space-y-1">
            <p class="text-[11px] text-amber-400">
              配不到源文件（种子已删、或源文件已不在下载器里），无法纳管
            </p>
            <p
              v-for="u in adoptResult.unmatched.slice(0, 5)"
              :key="u.link_path"
              class="truncate text-[11px] text-gray-600"
              :title="u.link_path"
            >
              {{ fileName(u.link_path) }}
            </p>
            <p v-if="adoptResult.unmatched.length > 5" class="text-[11px] text-gray-600">
              还有 {{ adoptResult.unmatched.length - 5 }} 条…
            </p>
          </div>

          <div v-if="(adoptResult.skipped || []).length" class="space-y-1">
            <p class="text-[11px] text-gray-500">跳过</p>
            <p
              v-for="(s, i) in adoptResult.skipped.slice(0, 5)"
              :key="i"
              class="truncate text-[11px] text-gray-600"
              :title="s.link_path"
            >
              {{ fileName(s.link_path) }} · {{ s.reason }}
            </p>
          </div>

          <p v-if="(adoptResult.errors || []).length" class="text-[11px] text-red-400">
            {{ adoptResult.errors.join('; ') }}
          </p>

          <p
            v-if="(adoptResult.adopted || []).length"
            class="rounded-lg bg-amber-950/40 px-3 py-2 text-[11px] text-amber-300"
          >
            登记后这些影片就进入删除联动范围：在 Emby 里删掉会连带删源文件与种子。
            请先核对上面的番号与配对是否合理。
          </p>
        </template>

        <div class="flex justify-end gap-2 pt-1">
          <button class="btn-ghost px-3 py-1.5 text-xs" @click="adoptOpen = false">
            取消
          </button>
          <button
            class="btn-primary px-3 py-1.5 text-xs"
            :disabled="adopting || !(adoptResult?.adopted || []).length"
            @click="runAdopt"
          >
            {{ adopting ? '登记中…' : '确认纳入管理' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
