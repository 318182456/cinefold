<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  deleteMediaLink, dropMediaLinkRecord, getMediaLinkStats, listMediaLinks,
  previewMediaLinkDelete, pruneMediaLinks, registerMediaLink,
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
const stats = ref(null)
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

async function load() {
  loading.value = true
  try {
    const data = await listMediaLinks({
      keyword: keyword.value.trim(),
      missing_only: missingOnly.value,
      page: page.value,
      size: size.value,
    })
    items.value = data.items || []
    total.value = data.total || 0
    deleteEnabled.value = !!data.delete_enabled
    libraryPath.value = data.library_path || ''
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

async function loadStats() {
  try {
    stats.value = await getMediaLinkStats()
  } catch {
    // 概览拿不到不影响列表
  }
}

function search() {
  page.value = 1
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
  load()
}

function changeSize(next) {
  const value = Number(next)
  if (!value || value === size.value) return
  size.value = value
  page.value = 1
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
    await Promise.all([load(), loadStats()])
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
    await Promise.all([load(), loadStats()])
  } catch (err) {
    toast.error(err.message)
  } finally {
    deleting.value = false
  }
}

async function dropRecord(item) {
  try {
    await dropMediaLinkRecord(item.link_path)
    toast.success('已删除记录')
    await Promise.all([load(), loadStats()])
  } catch (err) {
    toast.error(err.message)
  }
}

async function prune() {
  try {
    const data = await pruneMediaLinks()
    toast.success(`已清理 ${(data.removed || []).length} 条失效记录`)
    await Promise.all([load(), loadStats()])
  } catch (err) {
    toast.error(err.message)
  }
}

onMounted(() => {
  load()
  loadStats()
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
        <p
          class="mt-1 text-xl font-semibold"
          :class="stats?.missing ? 'text-amber-400' : 'text-gray-200'"
        >
          {{ stats?.missing ?? '—' }}
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
        class="btn-ghost px-3 py-1.5 text-xs"
        :class="missingOnly ? 'text-amber-300' : ''"
        @click="toggleMissing"
      >
        {{ missingOnly ? '显示全部' : '只看已丢失' }}
      </button>
      <button class="btn-ghost ml-auto px-3 py-1.5 text-xs" @click="prune">清理失效记录</button>
      <button class="btn-primary px-3 py-1.5 text-xs" @click="registerOpen = true">
        手工登记
      </button>
    </div>

    <LoadingBlock v-if="loading" :rows="5" />

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
  </div>
</template>
