<script setup>
import PagePanel from '@/components/PagePanel.vue'
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  listActors, subscribeActor, cancelActor, proxyImage, getActorProfile,
  refreshActorProfile, getActorProfileSync, subscribeActorWorks,
  startActorPhotos, getActorPhotos,
} from '@/api'
import { revealDetails } from '@/utils/statNavigation'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/EmptyState.vue'
import PrivateImage from '@/components/PrivateImage.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import CodeCard from '@/components/CodeCard.vue'

const toast = useToast(), route = useRoute(), router = useRouter()
const items = ref([]), loading = ref(false), directoryError = ref('')
const keyword = ref(''), directoryMode = ref('all'), directoryPage = ref(1), directoryTotal = ref(0)
const PAGE_SIZE = 24
const newName = ref(''), newDate = ref(''), subscriptionBusy = ref(new Set())
const selectedName = ref(''), profile = ref(null), works = ref([]), profileLoading = ref(false), profileError = ref('')
const worksPage = ref(1), worksSize = ref(PAGE_SIZE), worksTotal = ref(0), worksFilter = ref('all')
const sync = ref({ running: false, phase: 'idle', imported: 0, error: '' }), syncSubmitting = ref(false)
const syncPollError = ref(''), batchBusy = ref(false), batchResult = ref(null)
const photos = ref({ running: false }), photoError = ref('')
let photoTimer, photoBusy = false
function missingPhotos() {
  return selectedName.value ? (profile.value && (!actor.value.photo || actor.value.needs_name_lookup) ? [actorName.value] : [])
    : items.value.filter(item => !item.photo || item.needs_name_lookup).map(item => item.name)
}
async function fillPhotos(force = false) {
  if (disposed || photoBusy || photos.value.running) return
  const names = missingPhotos()
  if (!names.length) return
  photoBusy = true; photoError.value = ''
  try {
    photos.value = await startActorPhotos(names, force)
    if (!disposed && photos.value.running) schedulePhotoPoll()
  } catch (err) { if (!disposed) photoError.value = err.message }
  finally { photoBusy = false }
}
function schedulePhotoPoll() {
  clearTimeout(photoTimer)
  if (!disposed) photoTimer = setTimeout(pollPhotos, 1800)
}
async function pollPhotos() {
  try {
    const result = await getActorPhotos()
    if (disposed) return
    photos.value = result; photoError.value = ''
    if (result.running) schedulePhotoPoll()
    else {
      if (selectedName.value) await loadProfile()
      else await loadDirectory()
    }
  } catch (err) { if (!disposed) { photoError.value = err.message; schedulePhotoPoll() } }
}
let disposed = false, directoryRequest = 0, profileRequest = 0, refreshRequest = 0, batchRequest = 0, syncEpoch = 0, syncTimer
const directoryPages = computed(() => Math.max(1, Math.ceil(directoryTotal.value / PAGE_SIZE)))
const worksPages = computed(() => Math.max(1, Math.ceil(worksTotal.value / worksSize.value)))
const actor = computed(() => profile.value?.actor || { name: selectedName.value })
const actorName = computed(() => actor.value.name || selectedName.value)
const actorDisplayName = computed(() => actor.value.display_name || actorName.value)
const actorAliases = computed(() => Array.isArray(actor.value.aliases)
  ? actor.value.aliases.filter(name => typeof name === 'string' && name && name !== actorDisplayName.value) : [])
const sourceUrl = computed(() => {
  try { const url = new URL(actor.value.source_url); return ['http:', 'https:'].includes(url.protocol) ? url.href : '' }
  catch { return '' }
})
const fields = computed(() => [
  { label: '生日', value: actor.value.birthday },
  { label: '身高', value: actor.value.height ? `${actor.value.height}${/^\d+(\.\d+)?$/.test(String(actor.value.height)) ? ' cm' : ''}` : '' },
  { label: '三围', value: actor.value.measurements },
].filter(field => field.value))
const workFilters = computed(() => [
  { key: 'all', label: '已同步作品', count: profile.value?.stats?.total ?? '—' },
  { key: 'owned', label: '已入库', count: profile.value?.stats?.owned ?? '—' },
  { key: 'missing', label: '未入库', count: profile.value?.stats?.missing ?? '—' },
  { key: 'favorite', label: '收藏', count: profile.value?.stats?.favorite ?? '—' },
  { key: 'watched', label: '已观看', count: profile.value?.stats?.watched ?? '—' },
])
const SYNC_PHASES = {
  idle: '尚未更新', queued: '排队中', searching: '查找演员', profile: '同步演员资料',
  fetching: '读取作品', works: '同步作品', saving: '保存资料', running: '正在同步',
  completed: '更新完成', done: '更新完成', finished: '更新完成', failed: '更新失败', error: '更新失败', partial: '部分资料已同步',
}
const syncPhase = computed(() => SYNC_PHASES[sync.value.phase] || sync.value.phase || '尚未更新')
function isSubscriptionBusy(name) { return subscriptionBusy.value.has(name) }
function setSubscriptionBusy(name, busy) {
  const next = new Set(subscriptionBusy.value)
  busy ? next.add(name) : next.delete(name)
  subscriptionBusy.value = next
}
async function loadDirectory() {
  if (disposed) return
  const id = ++directoryRequest
  loading.value = true; directoryError.value = ''
  try {
    const result = await listActors({ keyword: keyword.value.trim(), page: directoryPage.value,
      size: PAGE_SIZE, subscribed: directoryMode.value === 'subscribed' })
    if (disposed || id !== directoryRequest) return
    items.value = result.items || []; directoryTotal.value = result.total || 0
    if (directoryPage.value > directoryPages.value) { directoryPage.value = directoryPages.value; await loadDirectory() }
    else if (!selectedName.value) fillPhotos()
  } catch (err) { if (!disposed && id === directoryRequest) directoryError.value = err.message }
  finally { if (!disposed && id === directoryRequest) loading.value = false }
}
function searchDirectory() { directoryPage.value = 1; items.value = []; directoryTotal.value = 0; loadDirectory() }
function setDirectoryMode(mode) { router.push({ query: { ...route.query, mode: mode === 'subscribed' ? 'subscribed' : undefined } }) }
function turnDirectoryPage(step) {
  directoryPage.value = Math.min(directoryPages.value, Math.max(1, directoryPage.value + step)); loadDirectory()
}
function openProfile(name) {
  const value = String(name || '').trim()
  if (value) router.push({ query: { ...route.query, name: value } })
}
async function openSearchProfile() {
  const value = keyword.value.trim()
  if (!value || loading.value) return
  const id = ++directoryRequest
  loading.value = true; directoryError.value = ''; directoryPage.value = 1
  try {
    const result = await listActors({ keyword: value, page: 1, size: PAGE_SIZE,
      subscribed: directoryMode.value === 'subscribed' })
    if (disposed || id !== directoryRequest) return
    items.value = result.items || []; directoryTotal.value = result.total || 0
    const normalize = name => String(name || '').normalize('NFKC').replace(/\s+/g, '').toLocaleLowerCase()
    const exact = items.value.find(item => [item.name, item.display_name, ...(item.aliases || [])]
      .some(name => normalize(name) === normalize(value)))
    if (exact || directoryTotal.value === 1) openProfile((exact || items.value[0]).name)
    else if (!directoryTotal.value) openProfile(value) // 尚无本地资料时保留完整姓名建档入口。
    else toast.info(`匹配到 ${directoryTotal.value} 位演员，请在下方选择具体演员。`)
  } catch (err) {
    if (!disposed && id === directoryRequest) directoryError.value = err.message
  } finally {
    if (!disposed && id === directoryRequest) loading.value = false
  }
}
function backToDirectory() { const query = { ...route.query }; delete query.name; router.push({ query }) }
function stopSyncPolling() { clearTimeout(syncTimer); syncTimer = undefined; syncEpoch += 1 }
function scheduleSyncPoll() {
  clearTimeout(syncTimer)
  if (!disposed && selectedName.value) syncTimer = setTimeout(pollSync, 1800)
}
async function loadProfile() {
  const name = selectedName.value
  if (!name || disposed) return
  const id = ++profileRequest, syncVersion = syncEpoch
  profileLoading.value = true; profileError.value = ''; works.value = []; worksTotal.value = 0
  try {
    const result = await getActorProfile({ name, page: worksPage.value, size: PAGE_SIZE, filter: worksFilter.value })
    if (disposed || id !== profileRequest || selectedName.value !== name) return
    profile.value = result; works.value = result.items || []; worksTotal.value = result.total || 0
    worksSize.value = result.size || PAGE_SIZE; worksPage.value = result.page || worksPage.value
    fillPhotos()
    if (syncVersion === syncEpoch) {
      sync.value = result.sync || { running: false, phase: 'idle', imported: 0, error: '' }
      if (sync.value.running) scheduleSyncPoll()
    }
  } catch (err) {
    if (!disposed && id === profileRequest && selectedName.value === name) profileError.value = err.message
  } finally {
    if (!disposed && id === profileRequest && selectedName.value === name) profileLoading.value = false
  }
}
async function pollSync() {
  clearTimeout(syncTimer)
  const name = selectedName.value, epoch = ++syncEpoch
  if (!name || disposed) return
  const canonical = actorName.value
  try {
    const result = await getActorProfileSync(canonical)
    if (disposed || selectedName.value !== name || epoch !== syncEpoch) return
    const wasRunning = sync.value.running
    sync.value = result; syncPollError.value = ''
    if (result.running) scheduleSyncPoll()
    else if (wasRunning) { await loadProfile(); loadDirectory() }
  } catch (err) {
    if (!disposed && selectedName.value === name && epoch === syncEpoch) { syncPollError.value = err.message; scheduleSyncPoll() }
  }
}
async function refreshProfile() {
  const name = selectedName.value
  if (!name || syncSubmitting.value || sync.value.running || profileLoading.value) return
  const canonical = actorName.value, id = ++refreshRequest
  stopSyncPolling(); profileRequest += 1
  syncSubmitting.value = true; syncPollError.value = ''
  try {
    const result = await refreshActorProfile(canonical, 3)
    if (disposed || selectedName.value !== name || id !== refreshRequest) return
    syncEpoch += 1
    sync.value = result
    if (result.running) scheduleSyncPoll()
    else { await loadProfile(); loadDirectory() }
    if (disposed || selectedName.value !== name || id !== refreshRequest) return
    if (result.error) toast.error(result.error)
    else toast.success(result.running ? '正在后台更新演员资料与作品' : '演员资料更新完成')
  } catch (err) {
    if (!disposed && selectedName.value === name && id === refreshRequest) {
      syncEpoch += 1
      sync.value = { ...sync.value, running: false, phase: 'failed', error: err.message }
    }
  } finally {
    if (!disposed && selectedName.value === name && id === refreshRequest) syncSubmitting.value = false
  }
}
function filterWorks(filter) { worksFilter.value = filter; worksPage.value = 1; loadProfile(); revealDetails('actor-work-results') }
function turnWorksPage(step) { worksPage.value = Math.min(worksPages.value, Math.max(1, worksPage.value + step)); loadProfile() }
async function changeSubscription(name, subscribed, limitDate = '') {
  if (!name || isSubscriptionBusy(name)) return false
  setSubscriptionBusy(name, true)
  try {
    if (subscribed) await subscribeActor(name, limitDate)
    else await cancelActor(name)
    if (disposed) return false
    items.value = items.value.map(item => item.name === name ? { ...item, subscribed } : item)
    if (profile.value?.actor?.name === name) profile.value.actor.subscribed = subscribed
    toast.success(`${subscribed ? '已订阅' : '已取消订阅'} ${name}`)
    loadDirectory()
    if (selectedName.value === name || profile.value?.actor?.name === name) await loadProfile()
    return true
  } catch (err) { if (!disposed) toast.error(err.message); return false }
  finally { if (!disposed) setSubscriptionBusy(name, false) }
}
async function addSubscription() {
  const name = newName.value.trim()
  if (await changeSubscription(name, true, newDate.value)) { newName.value = ''; newDate.value = '' }
}
async function subscribeWorks() {
  const name = selectedName.value
  if (!name || batchBusy.value || profileLoading.value || sync.value.running || syncSubmitting.value) return
  const canonical = actorName.value, id = ++batchRequest
  batchBusy.value = true
  try {
    const result = await subscribeActorWorks(canonical)
    if (disposed || selectedName.value !== name || id !== batchRequest) return
    batchResult.value = result
    toast.success(`已为 ${canonical} 新增 ${result.subscribed || 0} 部影片订阅`)
    await loadProfile()
  } catch (err) { if (!disposed && selectedName.value === name && id === batchRequest) toast.error(err.message) }
  finally { if (!disposed && selectedName.value === name && id === batchRequest) batchBusy.value = false }
}
watch(() => route.query.name, value => {
  stopSyncPolling(); profileRequest += 1; refreshRequest += 1; batchRequest += 1
  selectedName.value = String(Array.isArray(value) ? value[0] || '' : value || '').trim()
  profile.value = null; works.value = []; worksTotal.value = 0; worksPage.value = 1; worksFilter.value = 'all'
  profileError.value = ''; profileLoading.value = false; syncSubmitting.value = false; syncPollError.value = ''
  batchBusy.value = false; batchResult.value = null
  sync.value = { running: false, phase: 'idle', imported: 0, error: '' }
  if (selectedName.value) loadProfile()
}, { immediate: true })
watch(() => route.query.mode, value => { directoryMode.value = value === 'subscribed' ? 'subscribed' : 'all'; searchDirectory() }, { immediate: true })
onUnmounted(() => { disposed = true; clearTimeout(photoTimer); directoryRequest += 1; profileRequest += 1; refreshRequest += 1; batchRequest += 1; stopSyncPolling() })
</script>

<template>
  <PagePanel content-class="space-y-5">
    <div v-if="photos.running || photos.done || photoError" class="flex flex-wrap items-center gap-3 rounded-lg bg-gray-800/40 p-3 text-xs text-gray-400">
      <span>{{ photos.running ? '后台补全姓名与头像' : '姓名与头像补全结果' }} · {{ photos.done || 0 }}/{{ photos.total || 0 }} · 头像成功 {{ photos.ok || 0 }} · 头像未找到 {{ photos.failed || 0 }}</span>
      <span v-if="photos.updated_at">更新于 {{ new Date(photos.updated_at * 1000).toLocaleTimeString() }}</span>
      <span v-if="photoError" class="text-amber-400">{{ photoError }}</span>
      <button class="text-brand disabled:opacity-40" :disabled="photos.running || photoBusy" @click="fillPhotos(true)">重试当前姓名与头像补全</button>
    </div>
    <template v-if="!selectedName">
      <div class="flex flex-wrap items-end justify-between gap-3">
        <div><h1 class="text-lg font-medium text-gray-200">演员目录</h1><p class="mt-1 text-xs text-gray-500">查看本地演员与已同步作品，订阅演员可继续追踪新作品。</p></div>
        <div class="flex gap-2"><button class="btn-ghost !text-xs" :class="directoryMode === 'all' ? 'text-brand' : ''" :aria-pressed="directoryMode === 'all'" @click="setDirectoryMode('all')">全部演员</button><button class="btn-ghost !text-xs" :class="directoryMode === 'subscribed' ? 'text-brand' : ''" :aria-pressed="directoryMode === 'subscribed'" @click="setDirectoryMode('subscribed')">我的订阅</button></div>
      </div>
      <form class="flex flex-wrap gap-2" @submit.prevent="searchDirectory">
        <input v-model="keyword" class="input min-w-40 flex-1" placeholder="输入部分姓名或别名即可搜索" aria-label="搜索演员姓名或别名" maxlength="200" />
        <button class="btn-ghost" :disabled="loading">搜索目录</button><button type="button" class="btn-primary" :disabled="!keyword.trim() || loading" @click="openSearchProfile">查找并打开档案</button>
      </form>
      <p v-if="directoryError" class="text-xs text-red-400">{{ directoryError }}</p>
      <LoadingBlock v-if="loading" :rows="3" />
      <div v-else-if="items.length" class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        <article v-for="item in items" :key="item.name" class="card flex items-center gap-3 transition-colors hover:border-gray-700">
          <button class="h-14 w-14 shrink-0 overflow-hidden rounded-full bg-gray-800" :aria-label="`打开 ${item.display_name || item.name} 的档案`" @click="openProfile(item.name)"><PrivateImage :src="item.photo ? proxyImage(item.photo) : ''" :alt="item.display_name || item.name" fallback-text="暂无照片" img-class="h-full w-full object-cover" /></button>
          <div class="min-w-0 flex-1">
            <button class="max-w-full truncate text-left text-sm font-medium text-gray-200 hover:text-brand" @click="openProfile(item.name)">{{ item.display_name || item.name }}</button>
            <p class="mt-1 text-[10px] text-gray-600">{{ item.source === 'profile' ? 'JavDB 档案' : item.source === 'casts' ? '影片演员字段' : '已有演员资料' }}</p>
            <p v-if="item.aliases?.length || item.name_2" class="mt-1 truncate text-[11px] text-gray-500">{{ Array.isArray(item.aliases) && item.aliases.length ? item.aliases.join(' / ') : item.name_2 }}</p>
            <p v-if="item.limit_date && item.subscribed" class="mt-1 text-[11px] text-gray-500">{{ item.limit_date }} 起订阅</p>
            <div class="mt-2 flex flex-wrap gap-3"><button class="text-[11px] text-brand" @click="openProfile(item.name)">查看档案</button><button class="text-[11px] disabled:opacity-40" :class="item.subscribed ? 'text-gray-500' : 'text-brand'" :disabled="isSubscriptionBusy(item.name)" @click="changeSubscription(item.name, !item.subscribed)">{{ item.subscribed ? '取消订阅' : '订阅演员' }}</button></div>
          </div>
        </article>
      </div>
      <EmptyState v-else :text="directoryError ? '暂时无法读取演员目录' : keyword ? '没有匹配的本地演员' : directoryMode === 'subscribed' ? '还没有订阅演员' : '本地还没有演员资料'" hint="输入完整演员名可直接打开档案，再从 JavDB 更新资料和作品。" />
      <div v-if="directoryTotal" class="flex items-center justify-end gap-3 text-xs text-gray-500"><span>{{ directoryTotal }} 位演员 · {{ directoryPage }} / {{ directoryPages }} 页</span><button class="btn-ghost !text-xs" :disabled="loading || directoryPage <= 1" @click="turnDirectoryPage(-1)">上一页</button><button class="btn-ghost !text-xs" :disabled="loading || directoryPage >= directoryPages" @click="turnDirectoryPage(1)">下一页</button></div>
      <details class="card space-y-3"><summary class="cursor-pointer text-sm text-gray-400">按姓名添加演员订阅</summary><div class="flex flex-col gap-2 pt-3 sm:flex-row"><input v-model="newName" class="input sm:flex-1" placeholder="演员姓名" aria-label="订阅演员姓名" @keyup.enter="addSubscription" /><input v-model="newDate" type="date" class="input sm:!w-44" aria-label="订阅起始日期，可选" /><button class="btn-primary" :disabled="!newName.trim() || isSubscriptionBusy(newName.trim())" @click="addSubscription">添加订阅</button></div><p class="text-xs text-gray-600">可选起始日期，只追踪该日期之后发行的作品。</p></details>
    </template>
    <template v-else>
      <div class="flex flex-wrap items-center justify-between gap-3"><button class="text-xs text-brand" @click="backToDirectory">← 返回演员目录</button><button class="btn-ghost !text-xs" :disabled="profileLoading" @click="loadProfile">刷新本地档案</button></div>
      <section class="card space-y-4">
        <div class="flex flex-col gap-4 sm:flex-row sm:items-start">
          <div class="h-28 w-28 shrink-0 overflow-hidden rounded-xl bg-gray-800"><PrivateImage :src="actor.photo ? proxyImage(actor.photo) : ''" :alt="actorDisplayName" fallback-text="暂无照片" img-class="h-full w-full object-cover" /></div>
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-2"><h1 class="break-all text-xl font-medium text-gray-200">{{ actorDisplayName }}</h1><span v-if="actor.subscribed" class="rounded bg-brand/10 px-2 py-0.5 text-[10px] text-brand">已订阅新作</span></div>
            <p v-if="actorAliases.length" class="mt-2 break-all text-xs text-gray-400">别名 · {{ actorAliases.join(' / ') }}</p>
            <dl v-if="fields.length" class="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs"><div v-for="field in fields" :key="field.label" class="flex gap-2"><dt class="text-gray-500">{{ field.label }}</dt><dd class="text-gray-300">{{ field.value }}</dd></div></dl>
            <p v-if="actor.description" class="mt-3 whitespace-pre-line break-words text-xs leading-relaxed text-gray-400">{{ actor.description }}</p>
            <div class="mt-3 flex flex-wrap gap-3 text-[11px] text-gray-500"><span v-if="actor.updated_at">资料更新 {{ String(actor.updated_at).replace('T', ' ') }}</span><a v-if="sourceUrl" :href="sourceUrl" target="_blank" rel="noopener noreferrer" class="text-brand">JavDB 演员来源 ↗</a></div>
          </div>
          <div class="flex shrink-0 flex-wrap gap-2 sm:flex-col"><button class="btn-primary !text-xs" :disabled="syncSubmitting || sync.running || profileLoading" @click="refreshProfile">{{ syncSubmitting || sync.running ? 'JavDB 更新中…' : '从 JavDB 更新' }}</button><button class="btn-ghost !text-xs" :disabled="profileLoading || isSubscriptionBusy(actorName)" @click="changeSubscription(actorName, !actor.subscribed)">{{ actor.subscribed ? '取消演员追更' : '订阅演员新作' }}</button></div>
        </div>
        <p class="text-xs text-gray-500">JavDB 单次最多同步 3 页。下方目录和统计只覆盖本地已保存、已同步的作品，未表示该演员的完整片单。</p>
        <div v-if="sync.running || sync.phase !== 'idle' || sync.error || syncPollError" class="space-y-1 rounded-lg bg-gray-800/50 p-3 text-xs">
          <p :class="sync.error ? 'text-red-400' : 'text-gray-300'">{{ syncPhase }}<span v-if="sync.imported !== undefined"> · 本次导入 {{ sync.imported }} 部</span><span v-if="sync.remote_pages !== undefined"> · 读取 {{ sync.remote_pages }} 页</span></p>
          <p v-if="sync.running" class="text-gray-500">后台同步会继续执行，可以切换演员或离开页面。</p>
          <p v-if="sync.error" class="break-all text-red-400">JavDB 更新失败：{{ sync.error }}。已保存的本地档案和作品仍可查看。</p>
          <p v-if="syncPollError" class="break-all text-amber-400">暂时无法读取同步进度：{{ syncPollError }}；正在重试。</p>
          <p v-if="sync.truncated || sync.has_more" class="text-amber-400">本次只同步了部分页面，仍可能有尚未导入的作品。</p>
        </div>
        <p v-if="profileError" class="break-all text-xs text-red-400">本地档案读取失败：{{ profileError }}</p>
      </section>
      <div class="grid grid-cols-2 gap-3 sm:grid-cols-5"><button v-for="filter in workFilters" :key="filter.key" class="rounded-xl border p-3 text-left transition hover:bg-gray-800/50" :class="worksFilter === filter.key ? 'border-brand/50 bg-brand/5' : 'border-gray-800 bg-gray-900/40'" :aria-pressed="worksFilter === filter.key" @click="filterWorks(filter.key)"><p class="text-xs text-gray-500">{{ filter.label }}</p><p class="mt-2 text-xl font-medium tabular-nums text-gray-200">{{ filter.count }}</p></button></div>
      <section class="card space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-3"><h2 class="text-sm font-medium text-gray-300">订阅该演员已收录的未入库影片</h2><button class="btn-primary !text-xs" :disabled="batchBusy || profileLoading || sync.running || syncSubmitting" @click="subscribeWorks">{{ batchBusy ? '正在添加影片订阅…' : '一键订阅未入库影片' }}</button></div>
        <p class="text-xs text-gray-500">仅处理当前已收录作品，新资料可先从 JavDB 更新。已入库、下载中、已订阅或用户排除的影片会跳过；本次不直接推送下载。未来新作追更使用上方“订阅演员新作”。</p>
        <p v-if="batchResult" class="text-xs text-gray-400">本次匹配 {{ batchResult.matched || 0 }} 部，新增订阅 {{ batchResult.subscribed || 0 }} 部；已订阅 {{ batchResult.already_subscribed || 0 }}、已入库 {{ batchResult.owned || 0 }}、下载中 {{ batchResult.downloading || 0 }}、已排除 {{ batchResult.excluded || 0 }} 部已跳过。</p>
      </section>
      <section id="actor-work-results" class="scroll-mt-4 space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-2"><h2 class="text-sm font-medium text-gray-300">{{ workFilters.find(item => item.key === worksFilter)?.label }}<span v-if="!profileLoading" class="ml-2 text-xs text-gray-500">{{ worksTotal }} 部</span></h2><span class="text-xs text-gray-500">收藏和已观看状态来自本地已记录的数据</span></div>
        <LoadingBlock v-if="profileLoading" :rows="3" /><div v-else-if="works.length" class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3"><CodeCard v-for="item in works" :key="item.code" :item="item" @changed="loadProfile" /></div><EmptyState v-else :text="profileError ? '作品暂时无法读取' : '当前筛选下没有已保存作品'" hint="可切换筛选，或从 JavDB 更新演员资料与作品。" />
        <div v-if="worksTotal" class="flex items-center justify-end gap-3 text-xs text-gray-500"><span>{{ worksPage }} / {{ worksPages }} 页 · {{ worksTotal }} 部</span><button class="btn-ghost !text-xs" :disabled="profileLoading || worksPage <= 1" @click="turnWorksPage(-1)">上一页</button><button class="btn-ghost !text-xs" :disabled="profileLoading || worksPage >= worksPages" @click="turnWorksPage(1)">下一页</button></div>
      </section>
    </template>
  </PagePanel>
</template>
