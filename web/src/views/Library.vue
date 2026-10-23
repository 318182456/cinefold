<script setup>
import PagePanel from '@/components/PagePanel.vue'
import MobileSection from '@/components/MobileSection.vue'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import http, { codeCover, getConfig, saveConfig } from '@/api'
import { useToast } from '@/composables/useToast'
import PrivateImage from '@/components/PrivateImage.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import MetadataConditions from '@/components/MetadataConditions.vue'
const toast = useToast()
const route = useRoute(), router = useRouter()
const items = ref([]), total = ref(0), page = ref(1), busy = ref(false), collections = ref([]), selected = ref(null), recommendations = ref([])
const embyCollections = ref([]), embyCollectionsError = ref(''), systemCollections = ref([])
const activeSystem = computed(() => systemCollections.value.find(item => `system:${item.key}` === filters.value.collection_id))
const activeEmbyCollection = computed(() => embyCollections.value.find(item => `emby:${item.key}` === filters.value.collection_id))
const filters = ref({ keyword: '', genre: '', actor: '', publisher: '', favorite: false, emby_favorite: false, watch_later: false, unseen: false, watched: false, last_played_month: '', release_year: '', include_skipped: false, collection_id: '', sort: 'recent' })
const collectionOpen = ref(false), collectionForm = ref({ name: '', kind: 'manual', conditions: {} }), collectionEditing = ref(null), chosenCollection = ref('')
const embyOpen = ref(false), embyUsers = ref([]), embyUser = ref(''), embySyncing = ref(false)
const embyActing = ref(false)
async function embyAction(item, watch, action) {
  embyActing.value = true
  try {
    const result = await http.post(`/library/${encodeURIComponent(item.code)}/emby`, { item_id: watch.item_id, action })
    for (const target of [item, items.value.find(x => x.code === item.code)]) {
      if (!target) continue
      target.watches = target.watches.map(w => w.item_id === result.item_id && w.user_id === result.user_id ? result : w)
    }
    toast.success('Emby 已更新')
  } catch (e) { toast.error(e.message) } finally { embyActing.value = false }
}
let request = 0
async function load() {
  const id = ++request; busy.value = true
  try {
    const choice = String(filters.value.collection_id || '')
    const data = await http.get('/library', { params: { ...filters.value,
      collection_id: choice.includes(':') ? undefined : choice || undefined,
      system_collection: choice.startsWith('system:') ? choice.slice(7) : undefined,
      release_year: filters.value.release_year || undefined,
      emby_collection_key: choice.startsWith('emby:') ? choice.slice(5) : undefined, page: page.value } })
    if (id === request) { items.value = data.items; total.value = data.total }
  }
  catch (e) { if (id === request) { items.value = []; total.value = 0 }; toast.error(e.message) } finally { if (id === request) busy.value = false }
}
async function perform(fn) { try { await fn() } catch (e) { toast.error(e.message) } }
async function detail(item) { selected.value = JSON.parse(JSON.stringify(item)); recommendations.value = []; await perform(async () => { const code = item.code; const result = await http.get(`/library/${encodeURIComponent(code)}/similar`); if (selected.value?.code === code) recommendations.value = result }) }
async function preference(item, updates) { await perform(async () => { const p = await http.patch(`/library/${encodeURIComponent(item.code)}/preference`, updates); item.preference = p; const card = items.value.find(x => x.code === item.code); if (card) card.preference = p; toast.success('已保存') }) }
async function reloadCollections() {
  const [local, remote, system] = await Promise.allSettled([http.get('/collections'), http.get('/emby/collections/catalog'), http.get('/collections/system')])
  if (system.status === 'fulfilled') systemCollections.value = system.value
  else toast.error(system.reason.message)
  if (local.status === 'fulfilled') collections.value = local.value
  else toast.error(local.reason.message)
  if (remote.status === 'fulfilled') { embyCollections.value = remote.value.items || []; embyCollectionsError.value = '' }
  else embyCollectionsError.value = remote.reason.message
}
function chooseCollection(id) {
  filters.value.collection_id = id
  search()
}
function setCollectionState(key, checked) { const states = new Set(collectionForm.value.conditions.states || []); checked ? states.add(key) : states.delete(key); collectionForm.value.conditions.states = [...states] }
function newCollection(row = null) { collectionEditing.value = row?.id || null; collectionForm.value = row ? { ...JSON.parse(JSON.stringify(row)), conditions: { op: 'AND', sort: 'recent', states: [], ...JSON.parse(JSON.stringify(row.conditions || {})) } } : { name: '', kind: 'manual', conditions: { op: 'AND', sort: 'recent', states: [] } }; collectionOpen.value = true }
async function saveCollection() { await perform(async () => { const { name, kind, conditions } = collectionForm.value; await (collectionEditing.value ? http.put(`/collections/${collectionEditing.value}`, { name, kind, conditions }) : http.post('/collections', { name, kind, conditions })); collectionOpen.value = false; await reloadCollections(); await load() }) }
const activeCollection = computed(() => collections.value.find(x => x.id === Number(filters.value.collection_id)))
async function deleteCollection() { if (!activeCollection.value || !window.confirm(`删除片单「${activeCollection.value.name}」？影片与文件会保留。`)) return; await perform(async () => { await http.delete(`/collections/${activeCollection.value.id}`); await reloadCollections(); chooseCollection('') }) }
async function configureEmby() { embyOpen.value = true; await perform(async () => { const [users, config] = await Promise.all([http.get('/library/emby/users'), getConfig()]); embyUsers.value = users; embyUser.value = config.emby_user_id || '' }) }
async function syncEmby() { embySyncing.value = true; try { if (!embyUser.value) throw new Error('请先选择 Emby 用户'); await saveConfig({ emby_user_id: embyUser.value }); const result = await http.post('/library/emby/sync', null, { timeout: 600000 }); toast.success(`同步 ${result.matched} 条，${result.ignored} 条无法匹配本地番号`); embyOpen.value = false; await load() } catch (e) { toast.error(e.message) } finally { embySyncing.value = false } }
function search() {
  const query = {}
  for (const [key,value] of Object.entries(filters.value)) {
    if (value === false || value === '' || value == null || (key === 'sort' && value === 'recent')) continue
    query[key] = String(value)
  }
  if (router.resolve({ query }).fullPath === route.fullPath) { if (page.value !== 1) page.value = 1; else load() }
  else router.replace({ query })
}
watch(page, load)
watch(() => route.query, (query) => {
  for (const key of ['keyword', 'actor', 'genre', 'publisher', 'collection_id', 'last_played_month', 'release_year']) {
    filters.value[key] = typeof query[key] === 'string' ? query[key] : ''
  }
  for (const key of ['favorite', 'emby_favorite', 'watch_later', 'unseen', 'watched', 'include_skipped']) filters.value[key] = ['1', 'true'].includes(String(query[key] || ''))
  filters.value.sort = ['recent', 'release', 'rating', 'plays', 'size'].includes(query.sort) ? query.sort : 'recent'
  if (page.value !== 1) page.value = 1
  else load()
}, { immediate: true })
function clearFilters() {
  Object.assign(filters.value, { keyword: '', genre: '', actor: '', publisher: '', favorite: false, emby_favorite: false, watch_later: false, unseen: false, watched: false, last_played_month: '', release_year: '', include_skipped: false, collection_id: '', sort: 'recent' })
  if (Object.keys(route.query).length) router.replace({ name: 'Library', query: {} })
  else search()
}
onMounted(() => perform(reloadCollections))
</script>
<template>
  <PagePanel content-class="space-y-5">
    <template #header>

    <div class="flex flex-wrap items-center justify-between gap-3"><div><h1 class="text-xl font-semibold">媒体库</h1><p class="mt-1 text-sm text-gray-500">管理本地收藏与片单，在 Emby 中观看。</p></div><div class="flex flex-wrap gap-2"><button class="btn-ghost" @click="configureEmby">同步 Emby</button><button class="btn-primary" @click="newCollection()">新建片单</button></div></div>
    </template>

    <MobileSection title="合集与片单" :summary="`${collections.length + systemCollections.length} 个片单 · ${embyCollections.length} 个合集`" class="sm:card">
      <div class="flex items-center justify-between"><h2 class="text-sm font-medium text-gray-300">合集与片单 <span class="ml-2 text-xs text-gray-500">{{ collections.length + systemCollections.length }} 个片单 · {{ embyCollections.length }} 个 Emby 合集</span></h2><button class="text-xs text-brand" @click="reloadCollections">刷新列表</button></div>
      <div class="grid grid-cols-2 gap-2 lg:grid-cols-4"><button v-for="item in systemCollections" :key="item.key" class="rounded-lg border p-3 text-left" :class="filters.collection_id === `system:${item.key}` ? 'border-brand/60 bg-brand/5' : 'border-gray-800 hover:bg-gray-800/50'" @click="chooseCollection(`system:${item.key}`)"><p class="text-sm text-gray-300">{{ item.name }}</p><p class="mt-1 text-xs text-gray-500">{{ item.description }}</p></button></div>
      <div v-if="collections.length" class="space-y-2"><p class="text-xs text-gray-500">我的片单</p><div class="flex gap-2 overflow-x-auto pb-1"><button v-for="item in collections" :key="item.id" class="shrink-0 rounded-lg border px-4 py-3 text-left text-xs" :class="String(filters.collection_id) === String(item.id) ? 'border-brand/60 bg-brand/5' : 'border-gray-800 hover:bg-gray-800/50'" @click="chooseCollection(item.id)"><p class="text-gray-300">{{ item.name }}</p><p class="mt-1 text-gray-500">{{ item.kind === 'smart' ? '智能片单' : '手动片单' }}</p></button></div></div>
      <div v-if="embyCollections.length" class="space-y-2"><p class="text-xs text-gray-500">AI 合集 · Emby</p><div class="flex gap-2 overflow-x-auto pb-1"><button v-for="item in embyCollections" :key="item.key" class="flex max-w-64 shrink-0 gap-3 rounded-lg border p-2 text-left text-xs" :class="filters.collection_id === `emby:${item.key}` ? 'border-brand/60 bg-brand/5' : 'border-gray-800 hover:bg-gray-800/50'" @click="chooseCollection(`emby:${item.key}`)"><PrivateImage v-if="item.cover" :src="codeCover(item.cover)" :alt="item.name" img-class="h-14 w-10 rounded object-cover" :fallback-text="item.name" /><span class="py-1"><span class="line-clamp-2 text-gray-300">{{ item.name }}</span><span class="mt-1 block text-gray-500">{{ item.count }} 部 · Emby 合集</span></span></button></div></div>
      <p v-if="!collections.length && !embyCollections.length" class="text-xs text-gray-500">暂无片单或合集。可新建片单，或到任务页执行 AI 全库自动合集。</p>
      <p v-if="embyCollectionsError" class="text-xs text-amber-400">合集读取失败：{{ embyCollectionsError }}</p>
    </MobileSection>
    <MobileSection title="影片筛选" :summary="`${total} 部${filters.keyword ? ' · ' + filters.keyword : ''}`">
    <form class="card space-y-3" @submit.prevent="search">
      <div class="flex flex-wrap gap-3"><input v-model="filters.keyword" class="input !w-auto min-w-40 flex-1" placeholder="搜索番号、标题、演员、标签" aria-label="搜索媒体库"><select v-model="filters.collection_id" class="input !w-auto" aria-label="选择合集或片单" @change="chooseCollection(filters.collection_id)"><option value="">全部影片</option><optgroup label="智能片单"><option v-for="c in systemCollections" :key="c.key" :value="`system:${c.key}`">{{ c.name }}</option></optgroup><optgroup label="我的片单"><option v-for="c in collections" :key="c.id" :value="c.id">{{ c.kind === 'smart' ? '智能 · ' : '' }}{{ c.name }}</option></optgroup><optgroup label="Emby 合集"><option v-for="c in embyCollections" :key="c.key" :value="`emby:${c.key}`">{{ c.name }}</option></optgroup></select><select v-model="filters.sort" :disabled="!!activeSystem || activeCollection?.kind === 'smart'" class="input !w-auto" @change="search"><option value="recent">最近更新</option><option value="release">最新发行</option><option value="rating">个人评分</option><option value="plays">播放最多</option><option value="size">文件体积</option></select><button class="btn-ghost">搜索</button></div>
      <div class="flex flex-wrap items-center gap-4 text-sm"><label v-for="[key, label] in [['favorite','本地收藏'],['emby_favorite','Emby 收藏'],['watch_later','想看'],['unseen','未看完'],['watched','已看完'],['include_skipped','显示已跳过']]" :key="key" class="flex items-center gap-2"><input v-model="filters[key]" type="checkbox" @change="search">{{ label }}</label><span class="ml-auto text-gray-500">{{ total }} 部</span></div>
      <div class="flex flex-wrap gap-3">
        <input v-model="filters.genre" class="input !w-auto" placeholder="精确标签" aria-label="标签筛选">
        <input v-model="filters.actor" class="input !w-auto" placeholder="精确演员名" aria-label="演员筛选">
        <RouterLink v-if="filters.actor.trim()" :to="{ name: 'Actors', query: { name: filters.actor.trim() } }" class="btn-ghost text-xs">演员档案 ↗</RouterLink>
        <input v-model="filters.publisher" class="input !w-auto" placeholder="精确发行商" aria-label="发行商筛选">
        <button type="button" class="btn-ghost" @click="clearFilters">清除筛选</button>
      </div>
    </form>
    </MobileSection>
    <p v-if="filters.last_played_month || filters.release_year" class="text-xs text-brand">{{ filters.last_played_month ? `最后观看月份：${filters.last_played_month}` : '' }} {{ filters.release_year ? `发行年份：${filters.release_year}` : '' }}<button class="ml-3" @click="clearFilters">清除筛选</button></p>
    <p v-if="activeSystem" class="text-sm text-gray-400">智能片单 · {{ activeSystem.name }} · {{ activeSystem.description }}，随资料与观看状态自动更新</p>
    <div v-if="activeCollection" class="flex items-center gap-3 text-sm"><span class="text-gray-400">{{ activeCollection.kind === 'smart' ? '智能片单' : '手动片单' }} · {{ activeCollection.name }}</span><button class="text-brand" @click="newCollection(activeCollection)">编辑</button><button class="text-red-400" @click="deleteCollection">删除片单</button></div>
    <div v-if="activeEmbyCollection" class="flex flex-wrap items-center gap-3 text-sm"><span class="text-gray-400">Emby 合集 · {{ activeEmbyCollection.name }} · {{ activeEmbyCollection.count }} 部</span><a :href="activeEmbyCollection.emby_url" target="_blank" rel="noopener noreferrer" class="text-brand">在 Emby 中打开 ↗</a><RouterLink :to="{ name:'Task' }" class="text-xs text-gray-500">合集任务与进度</RouterLink></div>
    <p v-if="busy" class="py-8 text-center text-gray-500">加载中…</p>
    <div v-else-if="!items.length" class="card py-16 text-center text-gray-500">暂无影片。登记本地文件或同步 Emby 后会显示在这里。</div>
    <div v-else class="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-5">
      <article v-for="item in items" :key="item.code" class="overflow-hidden rounded-xl border border-gray-800 bg-gray-900/60">
        <button class="block aspect-[2/3] w-full overflow-hidden bg-gray-900" :aria-label="`查看 ${item.code}`" @click="detail(item)"><PrivateImage :src="codeCover(item)" :alt="item.code" img-class="h-full w-full object-cover" :object-position="item.portrait_side === 'left' ? 'left' : item.portrait_side === 'right' ? 'right' : 'center'" :fallback-text="item.code" /></button>
        <div class="space-y-2 p-3"><button class="text-sm font-medium text-brand" @click="detail(item)">{{ item.code }}</button><p class="line-clamp-2 min-h-10 text-xs text-gray-400">{{ item.cn_title || item.title || '暂无标题' }}</p><div class="flex items-center justify-between"><button :aria-label="item.preference.favorite ? '取消本地收藏' : '本地收藏'" :class="item.preference.favorite ? 'text-pink-400' : 'text-gray-600'" @click="preference(item, { favorite: !item.preference.favorite })">♥</button><span class="text-xs text-gray-500">{{ item.preference.rating ? `${item.preference.rating} 分` : item.watches.some(w => w.played) ? '已看完' : '未看完' }}</span><a v-if="item.emby_url" :href="item.emby_url" target="_blank" rel="noopener noreferrer" class="text-xs text-brand">Emby ↗</a></div></div>
      </article>
    </div>
    <div class="flex items-center justify-center gap-4"><button class="btn-ghost" :disabled="page <= 1 || busy" @click="page--">上一页</button><span class="text-sm text-gray-500">{{ page }} / {{ Math.max(1, Math.ceil(total / 30)) }}</span><button class="btn-ghost" :disabled="page * 30 >= total || busy" @click="page++">下一页</button></div>
    <ModalDialog :open="!!selected" @update:open="v => { if (!v) selected = null }" :title="selected?.code || ''" size="2xl" scrollable>
      <div v-if="selected" class="space-y-5">
        <h2 class="font-medium">{{ selected.cn_title || selected.title }}</h2><p class="text-sm text-gray-400">{{ selected.release_date }} · {{ selected.publisher }} · {{ selected.duration }} 分钟</p>
        <p class="whitespace-pre-line text-sm text-gray-400">{{ selected.outline || '暂无简介' }}</p><p class="text-sm text-gray-500">演员：{{ selected.casts || '未知' }}</p><p class="text-sm text-gray-500">标签：{{ selected.genres || '未知' }}</p>
        <div class="flex flex-wrap gap-2"><button class="btn-ghost" @click="preference(selected, { favorite: !selected.preference.favorite })">{{ selected.preference.favorite ? '取消本地收藏' : '本地收藏' }}</button><button class="btn-ghost" @click="preference(selected, { watch_later: !selected.preference.watch_later })">{{ selected.preference.watch_later ? '取消想看' : '想看' }}</button><button class="btn-ghost" @click="preference(selected, { skipped: !selected.preference.skipped })">{{ selected.preference.skipped ? '恢复展示' : '跳过此片' }}</button><a v-if="selected.emby_url" class="btn-primary" :href="selected.emby_url" target="_blank" rel="noopener noreferrer">在 Emby 中打开 ↗</a></div>
        <div v-if="selected.watches.length" class="space-y-3 rounded-lg border border-gray-800 p-3">
          <h3 class="text-sm font-medium">Emby 用户状态</h3>
          <p class="text-xs text-gray-500">以下按钮直接更新已选 Emby 用户；本地收藏与片单独立保留。手动标记已看不会生成播放日历记录。</p>
          <div v-for="w in selected.watches" :key="`${w.user_id}-${w.item_id}`" class="flex flex-wrap items-center gap-2">
            <span class="text-xs text-gray-500">条目 {{ w.item_id }} · 播放 {{ w.play_count }} 次</span>
            <button class="btn-ghost" :disabled="embyActing" @click="embyAction(selected, w, w.favorite ? 'unfavorite' : 'favorite')">{{ w.favorite ? '取消 Emby 收藏' : 'Emby 收藏' }}</button>
            <button class="btn-ghost" :disabled="embyActing" @click="embyAction(selected, w, w.played ? 'unplayed' : 'played')">{{ w.played ? '在 Emby 标记未看' : '在 Emby 标记已看' }}</button>
          </div>
        </div>
        <label class="block"><span class="label">我的评分</span><select :value="selected.preference.rating ?? ''" class="input !w-auto" @change="preference(selected, { rating: $event.target.value === '' ? null : Number($event.target.value) })"><option value="">未评分</option><option v-for="n in 11" :key="n" :value="n - 1">{{ n - 1 }} 分</option></select></label>
        <label class="block"><span class="label">笔记</span><textarea v-model="selected.preference.notes" class="input" rows="3" maxlength="5000" /></label><button class="btn-ghost" @click="preference(selected, { notes: selected.preference.notes || '' })">保存笔记</button>
        <div v-if="collections.some(c => c.kind === 'manual')" class="flex flex-wrap gap-2"><select v-model="chosenCollection" class="input !w-auto"><option value="">选择片单</option><option v-for="c in collections.filter(c => c.kind === 'manual')" :key="c.id" :value="c.id">{{ c.name }}</option></select><button class="btn-ghost" :disabled="!chosenCollection" @click="perform(async () => { await http.post(`/collections/${chosenCollection}/items`, { codes: [selected.code] }); toast.success('已加入片单') })">加入片单</button><button v-if="activeCollection?.kind === 'manual'" class="btn-ghost" @click="perform(async () => { await http.delete(`/collections/${activeCollection.id}/items/${encodeURIComponent(selected.code)}`); selected = null; await load() })">移出当前片单</button></div>
        <div v-for="w in selected.watches" :key="w.item_id" class="rounded-lg bg-gray-800 p-3 text-sm text-gray-400"><p>Emby：{{ w.played ? '已看完' : '未看完' }} · 累计 {{ w.play_count }} 次</p><p>进度 {{ Math.floor(w.position_ticks / 600000000) }} / {{ Math.floor(w.runtime_ticks / 600000000) }} 分钟</p><p v-if="w.last_played">最后观看：{{ w.last_played }}</p></div>
        <details v-if="selected.files.length" class="text-sm text-gray-400"><summary class="cursor-pointer">本地文件（{{ selected.files.length }}）</summary><p v-for="f in selected.files" :key="f.link_path" class="mt-2 break-all">{{ f.link_path }} · {{ f.size == null ? '大小未知' : `${(f.size / 1073741824).toFixed(2)} GB` }}</p></details>
        <div v-if="recommendations.length"><h3 class="mb-2 text-sm font-medium">相似影片</h3><div class="flex flex-wrap gap-2"><RouterLink v-for="r in recommendations" :key="r.code" :to="{ name: 'Search', query: { keyword: r.code } }" class="badge bg-gray-800 text-brand" @click="selected = null">{{ r.code }}</RouterLink></div></div>
      </div>
    </ModalDialog>
    <ModalDialog v-model:open="collectionOpen" title="片单" size="xl" scrollable><form class="space-y-4" @submit.prevent="saveCollection"><label><span class="label">名称</span><input v-model="collectionForm.name" class="input" required maxlength="128"></label><select v-model="collectionForm.kind" class="input" :disabled="!!collectionEditing"><option value="manual">手动收录</option><option value="smart">按条件自动收录</option></select><template v-if="collectionForm.kind === 'smart'"><MetadataConditions v-model="collectionForm.conditions" /><div class="grid gap-3 sm:grid-cols-2"><label><span class="label">条件组合</span><select v-model="collectionForm.conditions.op" class="input" aria-label="片单条件组合"><option value="AND">全部满足（AND）</option><option value="OR">满足任一（OR）</option></select></label><label><span class="label">最低个人评分</span><input v-model.number="collectionForm.conditions.min_rating" type="number" min="0" max="10" step="0.5" class="input" aria-label="最低个人评分" placeholder="不限"></label><label><span class="label">片单排序</span><select v-model="collectionForm.conditions.sort" class="input" aria-label="片单排序"><option v-for="[key,label] in [['recent','最近更新'],['added','最近入库'],['rating','个人评分'],['plays','播放最多'],['casts','出演人数'],['release','最新发行'],['size','文件体积']]" :key="key" :value="key">{{ label }}</option></select></label></div><div class="flex flex-wrap gap-4"><label v-for="[key,label] in [['favorite','本地收藏'],['watch_later','想看'],['watched','已看完'],['unwatched','未看完']]" :key="key" class="flex items-center gap-2 text-sm"><input type="checkbox" :checked="(collectionForm.conditions.states || []).includes(key)" @change="setCollectionState(key,$event.target.checked)">{{ label }}</label></div><p class="text-xs text-gray-500">最低评分单独生效；其余非空条件按组合方式匹配。片单自动更新，无需指定影片个数。</p></template><button class="btn-primary">保存片单</button></form></ModalDialog>
    <ModalDialog v-model:open="embyOpen" title="同步 Emby 观看状态"><div class="space-y-4"><p class="text-sm text-gray-400">选择用于统计的 Emby 用户。同步只读取 Emby，不修改播放进度；之后每 30 分钟自动更新。</p><select v-model="embyUser" class="input"><option value="">选择用户</option><option v-for="u in embyUsers" :key="u.id" :value="u.id">{{ u.name }}</option></select><button class="btn-primary" :disabled="embySyncing || !embyUser" @click="syncEmby">{{ embySyncing ? '同步中…' : '保存并同步' }}</button></div></ModalDialog>
  </PagePanel>
</template>
