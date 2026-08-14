<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  checkAllDataSources, checkDataSource, createDataSource, deleteDataSource,
  listDataSources, reorderDataSources, restoreDataSource, updateDataSource,
} from '@/api'
import { useToast } from '@/composables/useToast'
import LoadingBlock from '@/components/LoadingBlock.vue'

const toast = useToast()
const items = ref([])
const removed = ref([])
const loading = ref(false)
const checking = ref('')
const checkingAll = ref(false)
const editing = ref(null)
const draft = ref({})
// 显式清除 Cookie。不能靠「输入框留空」表达 —— 框里从不回显已有值，
// 留空是常态，那样每次只改间隔都会把 cookie 误清
const clearCookie = ref(false)
const creating = ref(false)
const newSource = ref({})
const removingKey = ref('')

const STATUS = {
  ok: { dot: 'bg-emerald-500', text: '正常' },
  blocked: { dot: 'bg-amber-500', text: '被拦截' },
  fail: { dot: 'bg-red-500', text: '不通' },
  '': { dot: 'bg-gray-600', text: '未测试' },
}

// 没有解析器的源只能测连通，不参与抓取，得让用户看清楚。
// 按 priority 排：这一组的次序就是抓取顺序，页面上要能看出来
const byPriority = (a, b) => a.priority - b.priority || a.key.localeCompare(b.key)
const usable = computed(
  () => items.value.filter((i) => i.has_parser).sort(byPriority),
)
const registered = computed(() => items.value.filter((i) => !i.has_parser))

async function load() {
  loading.value = true
  try {
    const data = await listDataSources()
    items.value = data.items || []
    removed.value = data.removed || []
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  newSource.value = {
    key: '', name: '', host: '', interval: 0, priority: 100, bypass_first: false,
  }
  creating.value = true
}

async function create() {
  const payload = { ...newSource.value }
  payload.interval = Number(payload.interval) || 0
  payload.priority = Number(payload.priority) || 100

  try {
    await createDataSource(payload)
    toast.success('已添加')
    creating.value = false
    await load()
  } catch (err) {
    toast.error(err.message)
  }
}

async function remove(item) {
  const tip = item.builtin
    ? `删除内置源「${item.name}」？之后可在页面底部恢复。`
    : `删除自定义源「${item.name}」？此操作不可恢复。`
  // eslint-disable-next-line no-alert
  if (!window.confirm(tip)) return

  removingKey.value = item.key
  try {
    await deleteDataSource(item.key)
    toast.success(`已删除 ${item.name}`)
    editing.value = null
    await load()
  } catch (err) {
    toast.error(err.message)
  } finally {
    removingKey.value = ''
  }
}

async function restore(entry) {
  try {
    await restoreDataSource(entry.key)
    toast.success(`已恢复 ${entry.name}`)
    await load()
  } catch (err) {
    toast.error(err.message)
  }
}

async function toggle(item) {
  const next = !item.enabled
  try {
    await updateDataSource(item.key, { enabled: next })
    item.enabled = next
  } catch (err) {
    toast.error(err.message)
  }
}

async function check(item) {
  checking.value = item.key
  try {
    const data = await checkDataSource(item.key)
    item.status = data.status
    item.status_message = data.message
    if (data.status === 'ok') toast.success(`${item.name} 连通正常`)
    else toast.error(`${item.name}：${data.message}`)
  } catch (err) {
    toast.error(err.message)
  } finally {
    checking.value = ''
  }
}

async function checkAll() {
  checkingAll.value = true
  try {
    const data = await checkAllDataSources()
    const byKey = Object.fromEntries((data.items || []).map((i) => [i.key, i]))
    items.value.forEach((item) => {
      const result = byKey[item.key]
      if (!result) return
      item.status = result.status
      item.status_message = result.message
    })
    const ok = (data.items || []).filter((i) => i.status === 'ok').length
    toast.success(`测试完成，${ok} / ${(data.items || []).length} 个正常`)
  } catch (err) {
    toast.error(err.message)
  } finally {
    checkingAll.value = false
  }
}

// 抓取时多个源并发跑、取最先返回的结果，排在前面的源先拿到并发额度，
// 所以顺序影响最终用哪个源的数据。这里只排「已接入抓取」那一组 ——
// 未接入解析的源不参与抓取，给它们排序没有意义
const reordering = ref(false)
// 正在拖的源，以及当前悬停到哪个源上（用来画插入位置的提示线）
const dragKey = ref('')
const dropKey = ref('')

/** 把 keys 的顺序写进本地 priority 并同步到后端。 */
async function applyOrder(keys) {
  const list = usable.value
  // 先在本地改 priority，usable 是按它排序的计算属性，页面立刻响应；
  // 失败时 load() 拉回真实顺序。起点沿用这一组现有的最小值，
  // 与后端一致，避免把整组顶到其他源前面
  const base = Math.min(...list.map((i) => i.priority))
  keys.forEach((key, index) => {
    const target = items.value.find((i) => i.key === key)
    if (target) target.priority = base + index
  })

  reordering.value = true
  try {
    await reorderDataSources(keys)
  } catch (err) {
    toast.error(err.message)
    await load()
  } finally {
    reordering.value = false
  }
}

function onDragStart(item, event) {
  // 上一次重排还在保存时不接受新的拖动：两次请求的顺序无法保证，
  // 后到的那个会把先到的覆盖掉
  if (reordering.value) {
    event.preventDefault()
    return
  }
  dragKey.value = item.key
  // 不设 dataTransfer 的话 Firefox 不会启动拖拽
  event.dataTransfer.effectAllowed = 'move'
  event.dataTransfer.setData('text/plain', item.key)
}

function onDragOver(item, event) {
  if (!dragKey.value || item.key === dragKey.value) return
  // 阻止默认行为才会触发 drop
  event.preventDefault()
  event.dataTransfer.dropEffect = 'move'
  dropKey.value = item.key
}

function onDragLeave(item, event) {
  // dragleave 在指针移到卡片内部的子元素上时也会触发，直接清会让提示线闪。
  // relatedTarget 是即将进入的元素，仍在卡内就不算离开
  if (event.currentTarget.contains(event.relatedTarget)) return
  if (dropKey.value === item.key) dropKey.value = ''
}

function onDragEnd() {
  dragKey.value = ''
  dropKey.value = ''
}

async function onDrop(target) {
  const from = usable.value.findIndex((i) => i.key === dragKey.value)
  const to = usable.value.findIndex((i) => i.key === target.key)
  onDragEnd()
  if (from < 0 || to < 0 || from === to) return

  const keys = usable.value.map((i) => i.key)
  keys.splice(to, 0, ...keys.splice(from, 1))
  await applyOrder(keys)
}

/** 键盘挪动。拖拽对键盘与读屏用户不可达，保留一条等价路径。 */
async function move(item, offset) {
  if (reordering.value) return
  const list = usable.value
  const from = list.findIndex((i) => i.key === item.key)
  const to = from + offset
  if (from < 0 || to < 0 || to >= list.length) return

  const keys = list.map((i) => i.key)
  keys.splice(to, 0, ...keys.splice(from, 1))
  await applyOrder(keys)
}

const editingItem = computed(
  () => items.value.find((i) => i.key === editing.value) || null,
)

function open(item) {
  editing.value = item.key
  clearCookie.value = false
  draft.value = {
    host: item.host,
    interval: item.interval,
    priority: item.priority,
    cookie: '',
    bypass_first: item.bypass_first,
  }
}

async function save() {
  const key = editing.value
  const payload = { ...draft.value }
  // Cookie 框打开时不回显已有值（避免泄露到页面上），所以「留空」只能
  // 表示「不修改」—— 否则每次只改间隔都会把 cookie 误清掉。
  // 想真的清除得走 clearCookie 这个显式开关
  if (clearCookie.value) {
    payload.cookie = ''
  } else if (!payload.cookie) {
    delete payload.cookie
  }
  payload.interval = Number(payload.interval) || 0
  payload.priority = Number(payload.priority) || 0

  try {
    await updateDataSource(key, payload)
    toast.success(clearCookie.value ? '已保存，Cookie 已清除' : '已保存')
    editing.value = null
    await load()
  } catch (err) {
    toast.error(err.message)
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center gap-2">
      <p class="text-xs text-gray-500">点击卡片可改地址、请求间隔与 Cookie</p>
      <button class="btn-ghost ml-auto px-3 py-1.5 text-xs" @click="openCreate">
        添加数据源
      </button>
      <button
        class="btn-ghost px-3 py-1.5 text-xs"
        :disabled="checkingAll"
        @click="checkAll"
      >
        {{ checkingAll ? '测试中…' : '测试全部' }}
      </button>
    </div>

    <LoadingBlock v-if="loading" :rows="4" />

    <template v-else>
      <!-- 已接入抓取的源 -->
      <div class="space-y-2">
        <p class="text-xs text-gray-500">
          已接入抓取
          <span class="text-gray-600">
            · 拖左侧手柄调整顺序（也可选中后按 ↑ ↓），靠前的源优先出结果
          </span>
        </p>
        <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          <div
            v-for="(item, index) in usable"
            :key="item.key"
            class="card cursor-pointer space-y-2 transition-colors hover:border-gray-700"
            :class="[
              dragKey === item.key ? 'opacity-40' : '',
              dropKey === item.key ? 'border-brand' : '',
            ]"
            @click="open(item)"
            @dragover="onDragOver(item, $event)"
            @dragleave="onDragLeave(item, $event)"
            @drop.prevent="onDrop(item)"
          >
            <div class="flex items-center gap-2">
              <!-- 手柄单独可拖：整卡可拖会吃掉点击（卡片点开设置）与文本选择 -->
              <button
                class="-ml-1 shrink-0 cursor-grab px-1 text-[11px] tabular-nums text-gray-600 hover:text-gray-300 active:cursor-grabbing"
                draggable="true"
                :title="`抓取顺序第 ${index + 1} 位，拖动可调整；也可用 ↑ ↓ 键`"
                @click.stop
                @dragstart="onDragStart(item, $event)"
                @dragend="onDragEnd"
                @keydown.up.prevent="move(item, -1)"
                @keydown.down.prevent="move(item, 1)"
              >
                <span class="mr-0.5 tracking-tighter text-gray-700">⠿</span>{{ index + 1 }}
              </button>
              <span class="h-2 w-2 shrink-0 rounded-full" :class="STATUS[item.status].dot" />
              <span class="truncate text-sm font-medium text-gray-200">{{ item.name }}</span>
              <span
                v-if="item.protected"
                class="badge shrink-0 bg-sky-950 text-sky-300"
                title="核心数据源，不可删除"
              >
                核心
              </span>
              <button
                class="ml-auto shrink-0"
                :title="item.enabled ? '已启用' : '已停用'"
                @click.stop="toggle(item)"
              >
                <span
                  class="flex h-4 w-8 items-center rounded-full px-0.5 transition-colors"
                  :class="item.enabled ? 'bg-brand' : 'bg-gray-700'"
                >
                  <span
                    class="h-3 w-3 rounded-full bg-white transition-transform"
                    :class="item.enabled ? 'translate-x-4' : ''"
                  />
                </span>
              </button>
            </div>

            <p class="truncate text-[11px] text-gray-500">{{ item.host }}</p>

            <div class="flex flex-wrap items-center gap-1.5">
              <span v-if="item.interval" class="badge bg-gray-800 text-gray-400">
                CD {{ item.interval }}s
              </span>
              <span v-if="item.bypass_first" class="badge bg-amber-950 text-amber-300">
                需过盾
              </span>
              <span v-if="item.has_cookie" class="badge bg-gray-800 text-gray-400">
                已配 Cookie
              </span>
              <button
                class="btn-ghost ml-auto px-2 py-0.5 text-[11px]"
                :disabled="checking === item.key"
                @click.stop="check(item)"
              >
                {{ checking === item.key ? '测试中' : '测试' }}
              </button>
              <button
                v-if="!item.protected"
                class="btn-ghost px-2 py-0.5 text-[11px] text-red-400 hover:bg-red-950"
                :disabled="removingKey === item.key"
                @click.stop="remove(item)"
              >
                删除
              </button>
            </div>

            <p v-if="item.status_message" class="text-[11px] text-gray-600">
              {{ item.status_message }}
            </p>
          </div>
        </div>
      </div>

      <!-- 仅登记，无解析器。内置源已全部接入解析，落到这里的基本是自定义源 -->
      <div v-if="registered.length" class="space-y-2 border-t border-gray-800 pt-4">
        <p class="text-xs text-gray-500">
          无解析器（可测连通性，但不参与抓取，因此不排序）
        </p>
        <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          <div
            v-for="item in registered"
            :key="item.key"
            class="card cursor-pointer space-y-2 opacity-70 transition-colors hover:border-gray-700 hover:opacity-100"
            @click="open(item)"
          >
            <div class="flex items-center gap-2">
              <span class="h-2 w-2 shrink-0 rounded-full" :class="STATUS[item.status].dot" />
              <span class="truncate text-sm text-gray-300">{{ item.name }}</span>
              <span
                v-if="!item.builtin"
                class="badge shrink-0 bg-gray-800 text-gray-400"
              >
                自定义
              </span>
              <button
                class="btn-ghost ml-auto shrink-0 px-2 py-0.5 text-[11px]"
                :disabled="checking === item.key"
                @click.stop="check(item)"
              >
                {{ checking === item.key ? '测试中' : '测试' }}
              </button>
              <button
                v-if="!item.protected"
                class="btn-ghost shrink-0 px-2 py-0.5 text-[11px] text-red-400 hover:bg-red-950"
                :disabled="removingKey === item.key"
                @click.stop="remove(item)"
              >
                删除
              </button>
            </div>
            <p class="truncate text-[11px] text-gray-500">{{ item.host }}</p>
            <div v-if="item.bypass_first">
              <span class="badge bg-amber-950 text-amber-300">需过盾</span>
            </div>
            <p v-if="item.status_message" class="text-[11px] text-gray-600">
              {{ item.status_message }}
            </p>
          </div>
        </div>
      </div>

      <!-- 已删除的内置源，随时可以找回来 -->
      <div v-if="removed.length" class="space-y-2 border-t border-gray-800 pt-4">
        <p class="text-xs text-gray-500">已删除的内置源（恢复会重置为默认配置）</p>
        <div class="flex flex-wrap gap-2">
          <button
            v-for="entry in removed"
            :key="entry.key"
            class="btn-ghost px-3 py-1 text-xs text-gray-400"
            @click="restore(entry)"
          >
            {{ entry.name }} <span class="text-gray-600">· 恢复</span>
          </button>
        </div>
      </div>
    </template>

    <!-- 详细设置 -->
    <div
      v-if="editing"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      @click.self="editing = null"
    >
      <div class="card w-full max-w-md space-y-3">
        <p class="text-sm font-medium text-gray-200">{{ editing }} 设置</p>

        <div>
          <label class="label">地址</label>
          <input v-model="draft.host" class="input" placeholder="https://example.com" />
        </div>

        <div class="grid gap-3 sm:grid-cols-2">
          <div>
            <label class="label">请求间隔（秒）</label>
            <input v-model="draft.interval" class="input" placeholder="0 表示用默认" />
          </div>
          <div>
            <label class="label">优先级</label>
            <input v-model="draft.priority" class="input" placeholder="越小越优先" />
          </div>
        </div>

        <div>
          <label class="label">Cookie</label>
          <textarea
            v-model="draft.cookie"
            class="input font-mono text-xs"
            :disabled="clearCookie"
            rows="2"
            placeholder="留空表示不修改"
          />
          <!-- 输入框不回显已有值，所以「留空」只能表示不修改。
               清除得有个显式开关，否则配错的 cookie 永远删不掉 -->
          <label
            v-if="editingItem?.has_cookie"
            class="mt-1.5 flex items-center gap-2 text-[11px] text-gray-400"
          >
            <input
              v-model="clearCookie"
              type="checkbox"
              class="h-3.5 w-3.5 accent-red-500"
            />
            清除已配置的 Cookie
          </label>
        </div>

        <label class="flex items-center gap-2">
          <input v-model="draft.bypass_first" type="checkbox" class="h-4 w-4 accent-emerald-500" />
          <span class="text-sm text-gray-400">直接走反爬绕过服务</span>
        </label>

        <div class="flex items-center gap-2 pt-1">
          <button
            v-if="editingItem && !editingItem.protected"
            class="btn-ghost px-3 py-1.5 text-xs text-red-400 hover:bg-red-950"
            :disabled="removingKey === editing"
            @click="remove(editingItem)"
          >
            删除
          </button>
          <span v-else-if="editingItem" class="text-[11px] text-gray-600">
            核心数据源不可删除
          </span>
          <button class="btn-ghost ml-auto px-3 py-1.5 text-xs" @click="editing = null">
            取消
          </button>
          <button class="btn-primary px-3 py-1.5 text-xs" @click="save">保存</button>
        </div>
      </div>
    </div>

    <!-- 新增自定义源 -->
    <div
      v-if="creating"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      @click.self="creating = false"
    >
      <div class="card w-full max-w-md space-y-3">
        <p class="text-sm font-medium text-gray-200">添加数据源</p>
        <p class="text-[11px] text-gray-600">
          自定义源没有解析器，只能做连通性测试，不参与抓取
        </p>

        <div class="grid gap-3 sm:grid-cols-2">
          <div>
            <label class="label">标识</label>
            <input v-model="newSource.key" class="input" placeholder="如 mysite" />
          </div>
          <div>
            <label class="label">显示名</label>
            <input v-model="newSource.name" class="input" placeholder="留空则用标识" />
          </div>
        </div>

        <div>
          <label class="label">地址</label>
          <input v-model="newSource.host" class="input" placeholder="https://example.com" />
        </div>

        <div class="grid gap-3 sm:grid-cols-2">
          <div>
            <label class="label">请求间隔（秒）</label>
            <input v-model="newSource.interval" class="input" placeholder="0 表示用默认" />
          </div>
          <div>
            <label class="label">优先级</label>
            <input v-model="newSource.priority" class="input" placeholder="越小越优先" />
          </div>
        </div>

        <label class="flex items-center gap-2">
          <input
            v-model="newSource.bypass_first"
            type="checkbox"
            class="h-4 w-4 accent-emerald-500"
          />
          <span class="text-sm text-gray-400">直接走反爬绕过服务</span>
        </label>

        <div class="flex justify-end gap-2 pt-1">
          <button class="btn-ghost px-3 py-1.5 text-xs" @click="creating = false">取消</button>
          <button class="btn-primary px-3 py-1.5 text-xs" @click="create">添加</button>
        </div>
      </div>
    </div>
  </div>
</template>
