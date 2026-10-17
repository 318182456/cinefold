<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import http from '@/api'
import { useToast } from '@/composables/useToast'
import ModalDialog from '@/components/ModalDialog.vue'

const props = defineProps({ open: { type: Boolean, default: false } })
const emit = defineEmits(['update:open', 'published'])
const toast = useToast()
const visible = computed({ get: () => props.open, set: value => emit('update:open', value) })
const mode = ref('all'), submitting = ref(false), currentKnown = ref(false), showGroups = ref(false)
const state = ref({ running: false, phase: 'idle', done: 0, total: 0, error: '', groups: [], token: '' })
const drafts = ref([]), draftToken = ref(''), requestError = ref(''), pollError = ref('')
let disposed = false, request = 0, timer, publishPending = false
const MODES = [
  { value: 'all', label: '全部分类一起分析' }, { value: 'series', label: '系列' },
  { value: 'theme', label: '主题' }, { value: 'sequel', label: '续作' },
  { value: 'actor', label: '演员' }, { value: 'style', label: '风格' },
]
const KINDS = { series: '系列', theme: '主题', sequel: '续作', actor: '演员', style: '风格' }
const PHASES = { idle: '尚未分析', suggest: '准备分析', suggesting: 'AI 分析中', analyzing: 'AI 分析中', ready: '等待确认',
  publishing: '正在发布到 Emby', published: '已发布到 Emby', completed: '处理完成', failed: '处理失败', partial: '部分合集未完成', interrupted: '任务已中断，可继续' }
const phase = computed(() => PHASES[state.value.phase] || state.value.phase || '尚未分析')
const elapsed = computed(() => {
  if (!state.value.started_at) return ''
  const seconds = Math.max(0, Math.floor(((state.value.running ? Date.now() / 1000 : state.value.finished_at) || state.value.updated_at || state.value.started_at) - state.value.started_at))
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
})
const locked = computed(() => submitting.value || state.value.running || !currentKnown.value)
const selected = computed(() => drafts.value.filter(group => group.enabled))
const selectedNames = computed(() => {
  const counts = new Map()
  for (const group of selected.value) {
    const name = group.name.normalize('NFKC').trim().toLocaleLowerCase()
    counts.set(name, (counts.get(name) || 0) + 1)
  }
  return counts
})
function groupError(group) {
  if (!group.serverCanPublish || group.missing.length) return '部分影片缺少有效 Emby 关联，请先同步后重新分析'
  if (!group.name.trim() || group.name.trim().length > 128) return '合集名称需要 1–128 个字符'
  if ((selectedNames.value.get(group.name.normalize('NFKC').trim().toLocaleLowerCase()) || 0) > 1) return '已选合集名称重复，请修改名称'
  if (new Set(group.picked).size < 2) return '每个合集至少保留 2 部影片'
  if (group.picked.length > 500) return '手动发布每组最多 500 部；全库自动创建可分批处理更大的合集'
  if (group.picked.some(code => !group.codes.includes(code))) return '成员已超出原预览范围，请重新分析'
  return ''
}
const invalidSelected = computed(() => selected.value.filter(group => groupError(group)))
const memberCount = computed(() => selected.value.reduce((count, group) => count + group.picked.length, 0))
const uniqueMemberCount = computed(() => new Set(selected.value.flatMap(group => group.picked)).size)
const canPublish = computed(() => !locked.value && !!draftToken.value && draftToken.value === state.value.token
  && selected.value.length > 0 && selected.value.length <= 40 && !invalidSelected.value.length)
const warnings = computed(() => Array.isArray(state.value.warnings) ? state.value.warnings : [])
const results = computed(() => (Array.isArray(state.value.groups) ? state.value.groups : [])
  .filter(group => ['published', 'failed'].includes(group.publish_status)))
function message(item) {
  if (typeof item === 'string') return item
  return item?.message || item?.reason || item?.error || JSON.stringify(item)
}
function confidence(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value <= 1 ? `${Math.round(value * 100)}%` : String(value)
  return value ? String(value) : ''
}
function makeDraft(group) {
  const codes = [...new Set((Array.isArray(group.codes) ? group.codes : []).filter(code => typeof code === 'string' && code))]
  const items = new Map((Array.isArray(group.items) ? group.items : []).map(item => [item.code, item]))
  const publishedCodes = Array.isArray(group.published_codes) ? group.published_codes.filter(code => codes.includes(code)) : []
  return { id: group.id, name: String(group.name || ''), kind: group.kind || '', reason: group.reason || '',
    confidence: group.confidence, codes, picked: publishedCodes.length >= 2 ? publishedCodes : [...codes], enabled: codes.length >= 2,
    publishStatus: group.publish_status || 'preview', publishError: group.publish_error || '', remoteId: group.remote_id,
    serverCanPublish: group.can_publish !== false, missing: Array.isArray(group.missing) ? group.missing : [],
    members: codes.map(code => ({ ...items.get(code), code })) }
}
function acceptState(next) {
  state.value = { ...state.value, ...next }
  currentKnown.value = true
  const groups = Array.isArray(next.groups) ? next.groups : []
  if ((!drafts.value.length && groups.length)
    || (next.token && Array.isArray(next.groups) && next.token !== draftToken.value)) {
    drafts.value = groups.map(makeDraft)
    draftToken.value = next.token || ''
  }
  const groupStates = new Map(groups.map(group => [group.id, group]))
  for (const group of drafts.value) {
    const latest = groupStates.get(group.id)
    if (!latest) continue
    group.publishStatus = latest.publish_status || 'preview'
    group.publishError = latest.publish_error || ''
    group.remoteId = latest.remote_id
    group.serverCanPublish = latest.can_publish !== false
    group.missing = Array.isArray(latest.missing) ? latest.missing : []
  }
  if (publishPending && !next.running && ['completed', 'published', 'partial', 'failed'].includes(next.phase)) {
    publishPending = false
    if (next.phase !== 'failed') emit('published')
  }
}
function stopPolling() { clearTimeout(timer); timer = undefined; request += 1 }
function schedulePoll(delay = 1800) {
  clearTimeout(timer)
  if (!disposed && props.open) timer = setTimeout(poll, delay)
}
async function poll() {
  clearTimeout(timer)
  const id = ++request
  try {
    const next = await http.get('/emby/collections/current')
    if (disposed || !props.open || id !== request) return
    pollError.value = ''; acceptState(next)
    if (next.running) schedulePoll()
  } catch (err) {
    if (!disposed && props.open && id === request) {
      currentKnown.value = false; pollError.value = err.message; schedulePoll(5000)
    }
  }
}
async function analyze(automatic = false) {
  if (locked.value || !MODES.some(item => item.value === mode.value)) return
  submitting.value = true; requestError.value = ''; publishPending = automatic; showGroups.value = false; stopPolling()
  try {
    const next = await http.post(automatic ? '/emby/collections/auto' : '/emby/collections/suggest', { mode: mode.value })
    if (disposed) return
    acceptState(next)
    if (props.open) await poll()
  } catch (err) {
    if (!disposed) { requestError.value = err.message; toast.error(err.message); currentKnown.value = false; schedulePoll(1000) }
  } finally { if (!disposed) submitting.value = false }
}
async function resume() {
  if (locked.value) return
  submitting.value = true; requestError.value = ''; publishPending = true; stopPolling()
  try {
    acceptState(await http.post('/emby/collections/resume'))
    if (props.open) await poll()
  } catch (err) { requestError.value = err.message; toast.error(err.message) }
  finally { submitting.value = false }
}
async function publish() {
  if (!canPublish.value) return
  const payload = { token: draftToken.value, groups: selected.value.map(group => ({
    id: group.id, name: group.name.trim(), codes: [...new Set(group.picked)],
  })) }
  submitting.value = true; requestError.value = ''; publishPending = true; stopPolling()
  try {
    const next = await http.post('/emby/collections/publish', payload)
    if (disposed) return
    acceptState(next)
    if (!next.running && (next.error || ['failed', 'partial'].includes(next.phase))) toast.error(next.error || '部分合集未完成，请查看逐项结果')
    else toast.success(next.running ? '正在后台发布 Emby 合集' : 'Emby 合集发布完成')
    if (props.open) await poll()
  } catch (err) {
    if (!disposed) { requestError.value = err.message; toast.error(err.message); currentKnown.value = false; schedulePoll(1000) }
  } finally { if (!disposed) submitting.value = false }
}
function selectGroups(enabled) { for (const group of drafts.value) group.enabled = enabled && group.codes.length >= 2 }
function selectMembers(group, enabled) { group.picked = enabled ? [...group.codes] : [] }
watch(() => props.open, open => { if (open) { currentKnown.value = false; poll() } else stopPolling() }, { immediate: true })
onUnmounted(() => { disposed = true; stopPolling() })
</script>

<template>
  <ModalDialog v-model:open="visible" title="AI 合集 · Emby" size="3xl" scrollable>
    <div class="space-y-4">
      <div class="space-y-1 text-xs leading-relaxed text-gray-400">
        <p>覆盖当前用户已同步到 Emby 的全部影片，按系列、主题、续作、演员与风格分批分析，无需填写影片数量。</p>
        <p>“全库后台自动创建”会直接创建或补充 Emby 合集；也可先生成建议，再手动修改和发布。同一影片可加入多个合集。</p>
      </div>
      <div class="flex flex-wrap items-end gap-3 rounded-lg bg-gray-800/40 p-3">
        <label class="min-w-44 flex-1 space-y-1 text-xs text-gray-400"><span>分类方式</span><select v-model="mode" class="input" :disabled="locked"><option v-for="item in MODES" :key="item.value" :value="item.value">{{ item.label }}</option></select></label>
        <button class="btn-primary !text-xs" :disabled="locked" @click="analyze(true)">全库后台自动创建</button>
        <button class="btn-ghost !text-xs" :disabled="locked" @click="analyze(false)">仅生成全库建议</button>
        <button class="btn-ghost !text-xs" :disabled="submitting" @click="poll">刷新状态</button>
      </div>
      <div v-if="state.phase !== 'idle' || state.running || pollError || requestError" class="space-y-2 rounded-lg border border-gray-700 p-3 text-xs">
        <p class="text-gray-300">{{ phase }}<span v-if="state.total"> · {{ state.done || 0 }} / {{ state.total }}</span></p>
        <p v-if="state.current" class="text-brand">{{ state.current }}</p>
        <p v-if="state.analyzed" class="text-gray-400">已分析 {{ state.analyzed }} / {{ state.scope?.included || state.analyzed }} 部影片</p>
        <p v-if="elapsed" class="text-gray-500">耗时 {{ elapsed }}<span v-if="state.updated_at"> · 最后更新 {{ new Date(state.updated_at * 1000).toLocaleTimeString() }}</span></p>
        <button v-if="state.can_resume" class="btn-primary !text-xs" :disabled="locked" @click="resume">继续任务 / 重试失败合集</button>
        <div v-if="state.running && state.total" class="h-1.5 overflow-hidden rounded-full bg-gray-800" role="progressbar" aria-label="AI 合集任务进度" :aria-valuenow="state.done || 0" :aria-valuemin="0" :aria-valuemax="Math.max(1, state.total)"><div class="h-full bg-brand transition-all" :style="{ width: `${Math.min(100, (state.done || 0) / state.total * 100)}%` }"></div></div>
        <p v-if="state.running" class="text-gray-500">任务会在后台继续。可以关闭窗口，返回后查看进度。</p>
        <p v-if="state.error" class="break-all text-red-400">{{ state.error }}</p>
        <p v-if="requestError" class="break-all text-red-400">{{ requestError }}</p>
        <p v-if="pollError" class="break-all text-amber-400">暂时无法读取状态：{{ pollError }}。请检查连接或设置，正在重试。</p>
        <p v-for="(warning, index) in warnings" :key="index" class="break-all text-amber-400">{{ message(warning) }}</p>
      </div>
      <div v-if="state.scope" class="space-y-1 text-xs text-gray-400"><p>本次覆盖 {{ state.scope.included ?? 0 }} 部 / 可关联影片 {{ state.scope.available ?? 0 }} 部<span v-if="!state.scope.truncated"> · 全部纳入</span>。</p><p v-if="state.scope.unmapped" class="text-amber-400">{{ state.scope.unmapped }} 部已入库影片缺少当前 Emby 关联，本次未纳入。请先同步 Emby 后重新分析。</p><p v-if="state.scope.truncated" class="text-amber-400">这是旧任务的有限范围，重新开始可覆盖全库。</p><p v-if="state.scope.description">{{ state.scope.description }}</p></div>
      <button v-if="state.automatic && drafts.length" class="text-xs text-brand" @click="showGroups = !showGroups">{{ showGroups ? '收起' : '查看' }} {{ drafts.length }} 个自动合集明细</button>
      <p v-if="drafts.length && draftToken !== state.token" class="text-xs text-amber-400">{{ state.running ? '当前保留上次预览，新分析完成后会更新候选。' : '当前保留上次预览；请重新分析取得有效预览后，再确认发布。' }}</p>
      <div v-if="drafts.length && (!state.automatic || showGroups)" class="space-y-3">
        <div v-if="!state.automatic" class="flex flex-wrap items-center gap-3 text-xs"><span class="text-gray-400">{{ drafts.length }} 个候选合集 · 已选 {{ selected.length }} 个</span><button class="text-brand disabled:opacity-40" :disabled="locked" @click="selectGroups(true)">全选合集</button><button class="text-gray-400 disabled:opacity-40" :disabled="locked" @click="selectGroups(false)">取消全选</button></div>
        <article v-for="group in drafts" :key="group.id" class="space-y-3 rounded-lg border p-3" :class="group.enabled ? 'border-brand/30 bg-brand/5' : 'border-gray-800'">
          <div class="flex flex-wrap items-start gap-3">
            <input v-if="!state.automatic" v-model="group.enabled" type="checkbox" class="mt-3" :disabled="locked || group.codes.length < 2" :aria-label="`选择合集 ${group.name}`" />
            <label v-if="!state.automatic" class="min-w-44 flex-1"><span class="sr-only">合集名称</span><input v-model="group.name" class="input" :disabled="locked" maxlength="128" aria-label="修改合集名称" /></label>
            <p v-else class="min-w-44 flex-1 pt-3 text-sm text-gray-200">{{ group.name }}</p>
            <span class="pt-3 text-xs text-gray-400">{{ KINDS[group.kind] || group.kind || '建议分类' }} · {{ group.picked.length }} / {{ group.codes.length }} 部</span>
          </div>
          <p v-if="group.reason" class="break-words text-xs leading-relaxed text-gray-400">建议理由：{{ group.reason }}</p>
          <p v-if="confidence(group.confidence)" class="text-[11px] text-gray-500">参考置信度 {{ confidence(group.confidence) }} · 请根据影片内容核对</p>
          <p v-if="!state.automatic && group.enabled && groupError(group)" class="text-xs text-amber-400">{{ groupError(group) }}</p>
          <p v-if="group.publishStatus === 'published'" class="text-xs text-emerald-400">上次已发布到 Emby<span v-if="group.remoteId"> · 合集 ID {{ group.remoteId }}</span></p>
          <p v-if="group.publishStatus === 'failed' || group.publishError" class="break-all text-xs text-red-400">发布失败：{{ group.publishError || '请查看任务错误并重试' }}</p>
          <p v-for="(missing, index) in group.missing" :key="index" class="break-all text-xs text-amber-400">缺少关联：{{ message(missing) }}</p>
          <details class="space-y-2 text-xs"><summary class="cursor-pointer text-gray-300">{{ state.automatic ? '查看合集成员' : '选择合集成员（至少 2 部）' }}</summary><div v-if="!state.automatic" class="flex gap-3 py-2"><button class="text-brand disabled:opacity-40" :disabled="locked" @click="selectMembers(group, true)">全选成员</button><button class="text-gray-400 disabled:opacity-40" :disabled="locked" @click="selectMembers(group, false)">清空选择</button></div><label v-for="item in group.members" :key="item.code" class="flex items-start gap-2 rounded bg-gray-800/40 p-2"><input v-if="!state.automatic" v-model="group.picked" type="checkbox" :value="item.code" :disabled="locked" class="mt-0.5" /><span class="min-w-0"><span class="font-mono text-brand">{{ item.code }}</span><span v-if="item.cn_title || item.title" class="ml-2 break-words text-gray-400">{{ item.cn_title || item.title }}</span></span></label></details>
        </article>
        <div v-if="!state.automatic" class="sticky bottom-0 space-y-2 border-t border-gray-800 bg-gray-900/95 py-3">
          <p class="text-xs text-gray-400">将发布 {{ selected.length }} 个合集，涉及 {{ uniqueMemberCount }} 部不同影片，合集成员合计 {{ memberCount }} 部。同片在不同合集分别保留。</p>
          <p v-if="invalidSelected.length" class="text-xs text-amber-400">请修正 {{ invalidSelected.length }} 个已选合集的名称或成员。</p>
          <p v-if="selected.length > 40" class="text-xs text-amber-400">每次最多发布 40 个合集，请减少所选合集。</p>
          <button class="btn-primary !text-xs" :disabled="!canPublish" @click="publish">确认发布所选合集到 Emby</button>
        </div>
      </div>
      <p v-else-if="!state.running && state.phase === 'ready'" class="py-6 text-center text-sm text-gray-500">当前分析范围没有生成可发布的合集建议。</p>
      <p v-else-if="!state.running && state.phase === 'published' && !drafts.length" class="py-6 text-center text-sm text-gray-500">全库分析完成，未发现符合条件的合集。</p>
      <p v-else-if="!state.running && state.phase === 'idle'" class="text-xs text-gray-500">点击全库后台自动创建即可开始，关闭窗口后任务仍会继续。</p>
      <details v-if="results.length" class="space-y-2 text-xs"><summary class="cursor-pointer text-gray-400">查看逐项发布结果</summary><div v-for="result in results" :key="result.id" class="space-y-1 border-t border-gray-800 pt-2"><p :class="result.publish_status === 'published' ? 'text-emerald-400' : 'text-red-400'">{{ result.name || result.id }} · {{ result.publish_status === 'published' ? '成功' : '未完成' }}</p><p v-if="result.publish_error" class="break-all text-red-400">{{ result.publish_error }}</p><p v-if="result.remote_id" class="text-gray-500">Emby 合集 ID {{ result.remote_id }}</p></div></details>
    </div>
  </ModalDialog>
</template>
