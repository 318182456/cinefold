<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { askAgent, cancelAgentAction, confirmAgentAction, getAgentStatus } from '@/api'
import { renderMarkdown } from '@/utils/markdown'

// 面板尺寸。拖动时要用它把面板夹在视口内，写死比读 DOM 省事且够准
const PANEL_W = 380
const PANEL_H = 520
const BALL = 48
const MARGIN = 12

// 只把最近若干轮问答回传后端，与后端的截断保持一致
const HISTORY_LIMIT = 12
const POS_KEY = 'agent_panel_pos'
const SUGGESTIONS = [
  '现在什么情况？',
  '下载卡住的任务有哪些？',
  '最近有什么报错吗？',
  '配置有什么问题？',
]

const open = ref(false)
const available = ref(false)
const model = ref('')
const question = ref('')
const sending = ref(false)
const messages = ref([])
const bodyRef = ref(null)
const inputRef = ref(null)

// 窄屏放不下固定尺寸，按视口留边收一下
const panelSize = ref({ w: PANEL_W, h: PANEL_H })

function measure() {
  panelSize.value = {
    w: Math.min(PANEL_W, window.innerWidth - MARGIN * 2),
    h: Math.min(PANEL_H, window.innerHeight - MARGIN * 2),
  }
}

// 悬浮球的位置。默认贴右下角，实际值在挂载时按视口算
const pos = ref({ x: 0, y: 0 })
const dragging = ref(false)
// 按下到抬起之间是否真的移动过。没移动就当点击，避免拖完手一松就弹开面板
let moved = false
let start = { x: 0, y: 0, px: 0, py: 0 }

function clamp(x, y) {
  const maxX = window.innerWidth - BALL - MARGIN
  const maxY = window.innerHeight - BALL - MARGIN
  return {
    x: Math.min(Math.max(x, MARGIN), Math.max(maxX, MARGIN)),
    y: Math.min(Math.max(y, MARGIN), Math.max(maxY, MARGIN)),
  }
}

/** 面板贴着球展开，并保证整体不出视口。 */
const panelStyle = computed(() => {
  const { w, h } = panelSize.value
  const left = pos.value.x + BALL / 2 > window.innerWidth / 2
    ? pos.value.x + BALL - w   // 球在右半屏：面板向左展开
    : pos.value.x
  const top = pos.value.y + BALL / 2 > window.innerHeight / 2
    ? pos.value.y - h - 8      // 球在下半屏：面板向上展开
    : pos.value.y + BALL + 8
  const maxLeft = window.innerWidth - w - MARGIN
  const maxTop = window.innerHeight - h - MARGIN
  return {
    left: `${Math.min(Math.max(left, MARGIN), Math.max(maxLeft, MARGIN))}px`,
    top: `${Math.min(Math.max(top, MARGIN), Math.max(maxTop, MARGIN))}px`,
    width: `${w}px`,
    height: `${h}px`,
  }
})

function onPointerDown(event) {
  dragging.value = true
  moved = false
  start = { x: event.clientX, y: event.clientY, px: pos.value.x, py: pos.value.y }
  window.addEventListener('pointermove', onPointerMove)
  window.addEventListener('pointerup', onPointerUp)
}

function onPointerMove(event) {
  if (!dragging.value) return
  const dx = event.clientX - start.x
  const dy = event.clientY - start.y
  // 几像素的抖动不算拖动，否则点击会被吃掉
  if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true
  pos.value = clamp(start.px + dx, start.py + dy)
}

function onPointerUp() {
  dragging.value = false
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
  if (moved) {
    localStorage.setItem(POS_KEY, JSON.stringify(pos.value))
  } else {
    open.value = !open.value
  }
}

function scrollToBottom() {
  nextTick(() => {
    const el = bodyRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

async function send(text) {
  const content = (text ?? question.value).trim()
  if (!content || sending.value) return

  question.value = ''
  messages.value.push({ role: 'user', content })
  sending.value = true
  scrollToBottom()

  // 只把纯问答带上去，附加的 tools 字段不属于对话内容
  const history = messages.value
    .slice(0, -1)
    .slice(-HISTORY_LIMIT)
    .map((m) => ({ role: m.role, content: m.content }))

  try {
    const data = await askAgent(content, history)
    messages.value.push({
      role: 'assistant',
      content: data?.answer || '没有返回内容',
      tools: data?.tools_used || [],
      // 待确认的下载器操作，点确认才真的执行
      proposals: (data?.proposals || []).map((p) => ({ ...p, status: 'pending' })),
    })
  } catch (err) {
    messages.value.push({
      role: 'assistant',
      content: `请求失败：${err.message}`,
      failed: true,
    })
  } finally {
    sending.value = false
    scrollToBottom()
  }
}

async function confirmProposal(proposal) {
  if (proposal.status !== 'pending') return
  proposal.status = 'running'
  try {
    const data = await confirmAgentAction(proposal.id)
    proposal.status = 'done'
    proposal.result = data?.message || `已${proposal.label}`
  } catch (err) {
    // 失败要能重试：下载器临时连不上是常见情况
    proposal.status = 'pending'
    proposal.error = err.message
  }
  scrollToBottom()
}

async function cancelProposal(proposal) {
  if (proposal.status !== 'pending') return
  try {
    await cancelAgentAction(proposal.id)
  } catch {
    // 后端没有这条提案也算取消成功，UI 照常收起
  }
  proposal.status = 'cancelled'
}

function onKeydown(event) {
  // Enter 发送，Shift+Enter 换行
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    send()
  }
}

function clearAll() {
  messages.value = []
}

function onResize() {
  measure()
  pos.value = clamp(pos.value.x, pos.value.y)
}

watch(open, (value) => {
  if (value) {
    scrollToBottom()
    nextTick(() => inputRef.value?.focus())
  }
})

onMounted(async () => {
  measure()
  const saved = localStorage.getItem(POS_KEY)
  if (saved) {
    try {
      const parsed = JSON.parse(saved)
      pos.value = clamp(parsed.x, parsed.y)
    } catch {
      pos.value = clamp(window.innerWidth, window.innerHeight)
    }
  } else {
    pos.value = clamp(window.innerWidth, window.innerHeight)
  }
  window.addEventListener('resize', onResize)

  try {
    const data = await getAgentStatus()
    // 后端没配好就不显示悬浮球，免得点开只看到一句"没配置"
    available.value = !!data?.enabled
    model.value = data?.model || ''
  } catch {
    available.value = false
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  window.removeEventListener('pointermove', onPointerMove)
  window.removeEventListener('pointerup', onPointerUp)
})
</script>

<template>
  <div v-if="available">
    <!-- 悬浮球：按下拖动，轻点开关面板 -->
    <button
      class="fixed z-50 flex items-center justify-center rounded-full bg-brand text-white shadow-lg
             transition-colors hover:bg-brand-hover"
      :class="dragging ? 'cursor-grabbing' : 'cursor-grab'"
      :style="{ left: `${pos.x}px`, top: `${pos.y}px`, width: `${BALL}px`, height: `${BALL}px`, touchAction: 'none' }"
      :title="open ? '收起助手' : 'AI 助手（可拖动）'"
      @pointerdown="onPointerDown"
    >
      <svg v-if="!open" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24">
        <path
          stroke-linecap="round"
          stroke-linejoin="round"
          d="M8 10h8M8 14h5m-1 7a9 9 0 100-18 9 9 0 000 18zm0 0l-3.5 2v-2"
        />
      </svg>
      <svg v-else class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <path stroke-linecap="round" d="M6 18L18 6M6 6l12 12" />
      </svg>
    </button>

    <!-- 对话面板 -->
    <div
      v-show="open"
      class="fixed z-50 flex flex-col overflow-hidden rounded-xl border border-gray-800
             bg-gray-900 shadow-2xl"
      :style="panelStyle"
    >
      <div class="flex h-11 shrink-0 items-center gap-2 border-b border-gray-800 px-3">
        <span class="h-1.5 w-1.5 rounded-full bg-brand" />
        <span class="text-sm font-medium">AI 助手</span>
        <span v-if="model" class="truncate text-[11px] text-gray-600">{{ model }}</span>
        <button
          v-if="messages.length"
          class="ml-auto text-[11px] text-gray-500 hover:text-gray-300"
          @click="clearAll"
        >
          清空
        </button>
        <button
          class="text-gray-500 hover:text-gray-300"
          :class="messages.length ? '' : 'ml-auto'"
          title="收起"
          @click="open = false"
        >
          <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
            <path stroke-linecap="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      <div ref="bodyRef" class="flex-1 space-y-3 overflow-y-auto p-3">
        <div v-if="!messages.length" class="space-y-3">
          <p class="text-xs leading-relaxed text-gray-500">
            可以直接问系统当前的情况。助手会实时查库、读日志、看任务，
            也能操作 qb / tr 的下载任务 —— 动手前会先让你确认。
          </p>
          <div class="flex flex-wrap gap-1.5">
            <button
              v-for="item in SUGGESTIONS"
              :key="item"
              class="rounded-full border border-gray-700 px-2.5 py-1 text-[11px] text-gray-400
                     hover:border-brand hover:text-brand"
              @click="send(item)"
            >
              {{ item }}
            </button>
          </div>
        </div>

        <div v-for="(msg, index) in messages" :key="index">
          <div v-if="msg.role === 'user'" class="flex justify-end">
            <div class="max-w-[85%] whitespace-pre-wrap break-words rounded-lg bg-brand/15 px-2.5 py-1.5 text-sm text-gray-100">
              {{ msg.content }}
            </div>
          </div>
          <div v-else class="space-y-1">
            <div
              class="agent-md max-w-full break-words rounded-lg bg-gray-800/70 px-2.5 py-1.5 text-sm"
              :class="msg.failed ? 'text-red-400' : 'text-gray-200'"
              v-html="renderMarkdown(msg.content)"
            />
            <!-- 待确认的下载器操作 -->
            <div
              v-for="proposal in msg.proposals || []"
              :key="proposal.id"
              class="rounded-lg border px-2.5 py-2 text-xs"
              :class="proposal.destructive
                ? 'border-red-900/70 bg-red-950/30'
                : 'border-gray-700 bg-gray-800/40'"
            >
              <p class="flex items-center gap-1.5">
                <svg
                  v-if="proposal.destructive"
                  class="h-3.5 w-3.5 shrink-0 text-red-400"
                  fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"
                >
                  <path
                    stroke-linecap="round" stroke-linejoin="round"
                    d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"
                  />
                </svg>
                <span :class="proposal.destructive ? 'font-medium text-red-300' : 'text-gray-300'">
                  {{ proposal.label }}
                </span>
                <span class="text-gray-500">· {{ proposal.targets.length }} 个任务</span>
              </p>

              <ul class="mt-1.5 space-y-0.5">
                <li
                  v-for="target in proposal.targets"
                  :key="target.hash"
                  class="flex items-baseline gap-1.5 text-gray-400"
                >
                  <span class="truncate">{{ target.name }}</span>
                  <span v-if="target.progress_percent !== null" class="shrink-0 text-[10px] text-gray-600">
                    {{ target.progress_percent }}%
                  </span>
                </li>
              </ul>

              <p v-if="proposal.destructive && proposal.status === 'pending'" class="mt-1.5 text-[11px] text-red-400">
                {{ proposal.action === 'delete_with_files'
                  ? '磁盘文件会一并删除，不可恢复'
                  : '只从下载器移除任务，磁盘文件保留' }}
              </p>
              <p v-if="proposal.error" class="mt-1.5 text-[11px] text-red-400">
                {{ proposal.error }}
              </p>

              <div v-if="proposal.status === 'pending'" class="mt-2 flex gap-1.5">
                <button
                  class="rounded px-2 py-1 text-[11px] font-medium text-white"
                  :class="proposal.destructive ? 'bg-red-600 hover:bg-red-700' : 'bg-brand hover:bg-brand-hover'"
                  @click="confirmProposal(proposal)"
                >
                  确认执行
                </button>
                <button
                  class="rounded border border-gray-700 px-2 py-1 text-[11px] text-gray-400 hover:bg-gray-800"
                  @click="cancelProposal(proposal)"
                >
                  取消
                </button>
              </div>
              <p v-else-if="proposal.status === 'running'" class="mt-2 text-[11px] text-gray-500">
                执行中…
              </p>
              <p v-else-if="proposal.status === 'done'" class="mt-2 text-[11px] text-brand">
                {{ proposal.result }}
              </p>
              <p v-else-if="proposal.status === 'cancelled'" class="mt-2 text-[11px] text-gray-500">
                已取消
              </p>
            </div>

            <p v-if="msg.tools?.length" class="flex flex-wrap items-center gap-1 px-1">
              <span class="text-[10px] text-gray-600">查询了</span>
              <span
                v-for="tool in msg.tools"
                :key="tool"
                class="rounded bg-gray-800 px-1.5 py-0.5 text-[10px] text-gray-500"
              >
                {{ tool }}
              </span>
            </p>
          </div>
        </div>

        <div v-if="sending" class="flex items-center gap-1.5 px-1 text-xs text-gray-500">
          <span class="h-1.5 w-1.5 animate-pulse rounded-full bg-brand" />
          正在查…
        </div>
      </div>

      <div class="shrink-0 border-t border-gray-800 p-2">
        <div class="flex items-end gap-1.5">
          <textarea
            ref="inputRef"
            v-model="question"
            rows="1"
            placeholder="问点什么…（Enter 发送）"
            class="input max-h-24 min-h-[38px] resize-none py-2 text-sm"
            @keydown="onKeydown"
          />
          <button class="btn-primary shrink-0 px-3 py-2" :disabled="sending || !question.trim()" @click="send()">
            <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" d="M12 19V5m0 0l-6 6m6-6l6 6" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* v-html 渲染的内容在 scoped 下拿不到组件作用域，用 :deep 穿透 */
.agent-md :deep(p) {
  margin: 0.25rem 0;
}
.agent-md :deep(p:first-child) {
  margin-top: 0;
}
.agent-md :deep(p:last-child) {
  margin-bottom: 0;
}
.agent-md :deep(ul),
.agent-md :deep(ol) {
  margin: 0.25rem 0;
  padding-left: 1.1rem;
}
.agent-md :deep(ul) {
  list-style: disc;
}
.agent-md :deep(ol) {
  list-style: decimal;
}
.agent-md :deep(li) {
  margin: 0.1rem 0;
}
.agent-md :deep(strong) {
  font-weight: 600;
  color: #fff;
}
.agent-md :deep(code) {
  border-radius: 3px;
  background: rgb(17 24 39);
  padding: 0.05rem 0.3rem;
  font-size: 0.8rem;
  color: #10b981;
}
/* 表格可能比面板宽，让它自己横向滚动，不要把面板撑开 */
.agent-md :deep(.md-table-wrap) {
  overflow-x: auto;
  margin: 0.35rem 0;
}
.agent-md :deep(table) {
  border-collapse: collapse;
  font-size: 0.75rem;
}
.agent-md :deep(th),
.agent-md :deep(td) {
  border: 1px solid rgb(55 65 81);
  padding: 0.2rem 0.45rem;
  text-align: left;
  white-space: nowrap;
}
.agent-md :deep(th) {
  background: rgb(31 41 55);
  font-weight: 500;
}
.agent-md :deep(a) {
  color: #10b981;
  text-decoration: underline;
}
</style>
