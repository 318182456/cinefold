<script setup>
import PagePanel from '@/components/PagePanel.vue'
import { onMounted, ref, watch } from 'vue'
import http from '@/api'
import { useToast } from '@/composables/useToast'
import ModalDialog from '@/components/ModalDialog.vue'
import MetadataConditions from '@/components/MetadataConditions.vue'
const toast = useToast()
const rules = ref([]), profiles = ref([]), busy = ref(false), open = ref(false), kind = ref('rule'), editing = ref(null), form = ref({}), preview = ref(null)
const assignment = ref({ code: '', profile_id: null })
let previewRequest = 0
watch(form, () => { preview.value = null; previewRequest++ }, { deep: true, flush: 'sync' })
watch(open, () => { previewRequest++ })
async function previewRule() {
  const request = ++previewRequest
  const body = JSON.parse(JSON.stringify({ name: form.value.name || '预览', conditions: form.value.conditions, profile_id: form.value.profile_id }))
  await perform(async () => {
    const result = await http.post('/subscription-rules/preview', body)
    if (open.value && request === previewRequest) preview.value = result
  })
}
const flags = [['only_chinese', '只要中字'], ['only_free', '只要免费'], ['only_uc', '只要无码'], ['exclude_uc', '排除无码'], ['only_uhd', '只要 4K'], ['exclude_uhd', '排除 4K'], ['only_vr', '只要 VR'], ['exclude_vr', '排除 VR']]
async function load() { [rules.value, profiles.value] = await Promise.all([http.get('/subscription-rules'), http.get('/profiles')]) }
async function perform(fn) { if (busy.value) return; busy.value = true; try { await fn() } catch (e) { toast.error(e.message) } finally { busy.value = false } }
function edit(type, row = null) {
  kind.value = type; editing.value = row?.id || null; preview.value = null
  form.value = row ? JSON.parse(JSON.stringify(row)) : type === 'rule' ? { name: '', enabled: false, conditions: {}, profile_id: null } : { name: '', filters: { exclude_vr: true }, sort: 'free,chinese,seeders' }
  open.value = true
}
async function save() { await perform(async () => {
  const path = kind.value === 'rule' ? '/subscription-rules' : '/profiles'
  const { name, enabled, conditions, profile_id, filters, sort } = form.value
  if (filters) { filters.min_seeders = Number(filters.min_seeders) || 0; filters.max_seeders = Number(filters.max_seeders) || 0 }
  const body = kind.value === 'rule' ? { name, enabled, conditions, profile_id } : { name, filters, sort }
  await (editing.value ? http.put(`${path}/${editing.value}`, body) : http.post(path, body))
  open.value = false; await load(); toast.success('已保存')
}) }
async function remove(type, row) { if (!window.confirm(`删除「${row.name}」？`)) return; await perform(async () => { await http.delete(`/${type}/${row.id}`); await load() }) }
onMounted(() => perform(load))
</script>
<template>
  <PagePanel content-class="space-y-6">
    <template #header>

    <div class="flex flex-wrap items-center justify-between gap-3"><div><h1 class="text-xl font-semibold">条件订阅</h1><p class="mt-1 text-sm text-gray-500">从资料库匹配新片，随订阅下载任务自动执行。多条规则命中时使用最早创建的规则。</p></div><button class="btn-primary" :disabled="busy" @click="edit('rule')">新建规则</button></div>
    </template>

    <div class="card space-y-3">
      <p v-if="!rules.length" class="text-sm text-gray-500">还没有规则。可以追踪标签、厂牌、系列或番号前缀。</p>
      <div v-for="row in rules" :key="row.id" class="flex flex-wrap items-center gap-3 border-b border-gray-800 py-3 last:border-0">
        <span class="badge" :class="row.enabled ? 'bg-green-500/10 text-green-400' : 'bg-gray-800 text-gray-400'">{{ row.enabled ? '启用' : '停用' }}</span>
        <div class="min-w-0 flex-1"><p>{{ row.name }}</p><p class="text-xs text-gray-500">{{ profiles.find(p => p.id === row.profile_id)?.name || '全局下载方案' }}</p></div>
        <button class="btn-ghost" @click="edit('rule', row)">编辑</button><button class="btn-ghost" :disabled="busy" @click="perform(async () => { await http.put(`/subscription-rules/${row.id}`, { name: row.name, conditions: row.conditions, profile_id: row.profile_id, enabled: !row.enabled }); await load() })">{{ row.enabled ? '停用' : '启用' }}</button>
        <button class="btn-ghost" :disabled="busy" @click="remove('subscription-rules', row)">删除</button>
      </div>
      <button class="btn-ghost" :disabled="busy || !rules.some(r => r.enabled)" @click="perform(async () => { const r = await http.post('/subscription-rules/run'); toast.success(`新增订阅 ${r.subscribed} 部`) })">立即匹配并订阅</button>
    </div>
    <div class="flex items-center justify-between"><h2 class="text-lg font-medium">下载方案</h2><button class="btn-ghost" @click="edit('profile')">新建方案</button></div>
    <div class="grid gap-3 md:grid-cols-2"><div v-for="p in profiles" :key="p.id" class="card"><p class="font-medium">{{ p.name }}</p><p class="my-3 break-all text-xs text-gray-500">排序：{{ p.sort || '原始顺序' }}</p><div class="flex gap-2"><button class="btn-ghost" @click="edit('profile', p)">编辑</button><button class="btn-ghost" :disabled="busy" @click="remove('profiles', p)">删除</button></div></div></div>
    <form class="card flex flex-wrap items-end gap-3" @submit.prevent="perform(async () => { await http.post('/profiles/assign', assignment); toast.success('已更新该番号的下载方案') })">
      <label class="min-w-40 flex-1"><span class="label">单独指定番号方案</span><input v-model="assignment.code" class="input" required placeholder="SONE-001"></label>
      <select v-model="assignment.profile_id" class="input !w-auto"><option :value="null">全局方案</option><option v-for="p in profiles" :key="p.id" :value="p.id">{{ p.name }}</option></select><button class="btn-ghost" :disabled="busy">应用</button>
    </form>
    <ModalDialog v-model:open="open" :title="kind === 'rule' ? '条件订阅规则' : '下载方案'" size="xl" scrollable>
      <form class="space-y-4" @submit.prevent="save">
        <label><span class="label">名称</span><input v-model="form.name" class="input" required maxlength="128"></label>
        <template v-if="kind === 'rule'">
          <MetadataConditions v-model="form.conditions" />
          <label><span class="label">下载方案</span><select v-model="form.profile_id" class="input"><option :value="null">全局方案</option><option v-for="p in profiles" :key="p.id" :value="p.id">{{ p.name }}</option></select></label>
          <label class="flex items-center gap-2 text-sm"><input v-model="form.enabled" type="checkbox">启用自动订阅</label>
          <button type="button" class="btn-ghost" :disabled="busy" @click="previewRule">预览匹配</button>
          <div v-if="preview" class="rounded-lg bg-gray-800 p-3 text-sm"><p>共匹配 {{ preview.total }} 部，可新增 {{ preview.eligible }} 部</p><p class="mt-1 text-xs text-gray-500">已有订阅或下载记录 {{ preview.existing }} · 手动排除 {{ preview.excluded }} · 下载方案过滤 {{ preview.filtered }}。预览最多列出 50 部；实际执行还会遵循其他规则的先后顺序。</p><p class="mt-2 break-all text-gray-400">{{ preview.items.map(x => x.code).join('、') || '暂无匹配影片' }}</p></div>
        </template>
        <template v-else>
          <div class="grid grid-cols-2 gap-3"><label v-for="[key, label] in flags" :key="key" class="flex items-center gap-2 text-sm"><input v-model="form.filters[key]" type="checkbox">{{ label }}</label></div>
          <div class="grid gap-3 sm:grid-cols-2"><label v-for="[key, label] in [['min_size','最小体积（如 2GB）'],['max_size','最大体积'],['include_keywords','包含关键词（逗号分隔）'],['exclude_keywords','排除关键词']]" :key="key"><span class="label">{{ label }}</span><input v-model="form.filters[key]" class="input"></label>
          <label v-for="[key, label] in [['min_seeders','最少做种数'],['max_seeders','最多做种数']]" :key="key"><span class="label">{{ label }}</span><input v-model.number="form.filters[key]" class="input" type="number" min="0"></label></div>
          <label><span class="label">排序（优先项在前，! 表示反向）</span><input v-model="form.sort" class="input" placeholder="free,chinese,seeders"><span class="text-xs text-gray-500">支持 free、chinese、uc、uhd、vr、seeders、size、site</span></label>
        </template>
        <div class="flex justify-end gap-2"><button type="button" class="btn-ghost" @click="open = false">取消</button><button class="btn-primary" :disabled="busy">保存</button></div>
      </form>
    </ModalDialog>
  </PagePanel>
</template>
