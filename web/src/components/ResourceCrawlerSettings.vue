<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { getConfig, saveConfig, previewSchedule } from '@/api'
import { useVisibleTimers } from '@/composables/useVisibleTimers'
import { useToast } from '@/composables/useToast'

const props = defineProps({ jobs: { type: Array, default: () => [] }, sources: { type: Array, default: () => [] } })
const emit = defineEmits(['saved'])
const toast = useToast(), loading = ref(true), saving = ref(false), error = ref('')
const form = reactive({}), baseline = reactive({})
const previews = reactive({}), timers = {}, revisions = {}
const { setTimeout, clearTimeout } = useVisibleTimers()
let disposed = false
const examples = ['每 2 小时', '每天凌晨 4 点', '每周一早上 9 点', '关闭自动抓取']
const fields = [
  { key: 'resource_avbase_date', label: 'Avbase 抓取日期', type: 'date', value: '', hint: '留空取今天，设置后执行任务“按日期抓取 Avbase 新片”' },
  { key: 'resource_catalog_enabled', label: '搜索包含本地资源库', type: 'checkbox', value: true, hint: '搜索时包含原生爬虫采集到的磁链' },
  { key: 'bt_auto_download', label: '本地资源参与自动下载', type: 'checkbox', value: true, hint: '关闭后仍可搜索和手动下载' },
  { key: 'resource_crawl_time', label: '资源抓取时间', value: '', job: 'crawl_resources', hint: '用自然语言填写，留空或填写“关闭自动抓取”则只允许手动执行' },
  { key: 'resource_metadata_time', label: '影片与演员抓取时间', value: '', job: 'crawl_resource_metadata', hint: 'Avbase 新片、JavDB / JavLibrary 榜单及演员，支持自然语言周期' },
  { key: 'resource_crawl_pages', label: '每板块最多页数', type: 'number', value: 2, min: 1, max: 100 },
  { key: 'resource_crawl_limit', label: '每轮帖子上限', type: 'number', value: 50, min: 1, max: 1000, hint: '达到上限后可继续任务' },
  { key: 'resource_sht_sections', label: '色花堂板块', type: 'sections', source: 'sehuatang', value: '37,103,104' },
  { key: 'resource_x1080_sections', label: 'X1080X 板块', type: 'sections', source: 'x1080x', value: '5206,5479,5208' },
]
const changes = computed(() => Object.fromEntries(fields.filter(field => form[field.key] !== baseline[field.key]).map(field => [field.key, form[field.key]])))
const dirty = computed(() => Object.keys(changes.value).length > 0)
const sectionChoices = field => props.sources.find(source => source.key === field.source)?.sections || {}
const selectedSections = field => String(form[field.key] || '').split(',').filter(Boolean)
function toggleSection(field, key, checked) {
  const selected = new Set(selectedSections(field))
  if (checked) selected.add(key); else selected.delete(key)
  form[field.key] = [...selected].join(',')
}
async function load() {
  loading.value = true; error.value = ''
  try {
    const config = await getConfig()
    const values = {}
    await Promise.all(fields.map(async field => {
      const value = config[field.key] ?? field.value
      if (!field.job || !value) { values[field.key] = value; return }
      const job = props.jobs.find(job => job.id === field.job && job.schedule === value)
      values[field.key] = job?.schedule_text || (await previewSchedule(field.job, value)).schedule_text
    }))
    if (!disposed) { Object.assign(form, values); Object.assign(baseline, values) }
  } catch (e) { error.value = e.message }
  finally { loading.value = false }
}
// Task polling may update a schedule edited through “改周期”. Keep untouched
// fields in sync, without replacing an in-progress edit or unrelated settings.
watch(() => props.jobs, jobs => {
  if (loading.value || saving.value) return
  for (const field of fields.filter(field => field.job)) {
    const job = jobs.find(job => job.id === field.job)
    if (job && form[field.key] === baseline[field.key]) form[field.key] = baseline[field.key] = job.schedule ? job.schedule_text : ''
  }
})
function scheduleInput(field) {
  clearTimeout(timers[field.key])
  const revision = revisions[field.key] = (revisions[field.key] || 0) + 1
  const text = form[field.key].trim()
  previews[field.key] = text ? { pending: true } : { text: '不自动执行，可手动抓取' }
  if (!text) return
  timers[field.key] = setTimeout(async () => {
    try {
      const result = await previewSchedule(field.job, text)
      if (!disposed && revision === revisions[field.key]) previews[field.key] = { text: result.schedule_text }
    } catch (e) {
      if (!disposed && revision === revisions[field.key]) previews[field.key] = { error: e.message }
    }
  }, 400)
}
async function save() {
  if (!dirty.value || saving.value) return
  saving.value = true; error.value = ''
  const updates = { ...changes.value }
  try {
    for (const field of fields.filter(field => field.type === 'sections')) if (!selectedSections(field).length) throw new Error(`请至少选择一个${field.label}`)
    const submitted = { ...updates }, readable = {}
    // Resolve every changed schedule before writing any configuration, so a
    // bad phrase cannot partially save the other schedule or normal settings.
    await Promise.all(fields.filter(field => field.job && field.key in updates).map(async field => {
      const text = String(updates[field.key] || '').trim()
      try {
        const result = text ? await previewSchedule(field.job, text) : { schedule: '', schedule_text: '' }
        updates[field.key] = result.schedule
        readable[field.key] = result.schedule ? result.schedule_text : ''
      } catch (e) { throw new Error(`${field.label}：${e.message}`) }
    }))
    await saveConfig(updates)
    for (const key of Object.keys(submitted)) form[key] = baseline[key] = key in readable ? readable[key] : submitted[key]
    for (const field of fields.filter(field => field.job)) { clearTimeout(timers[field.key]); revisions[field.key] = (revisions[field.key] || 0) + 1; delete previews[field.key] }
    toast.success('资源抓取设置已保存')
    emit('saved')
  } catch (e) { error.value = e.message }
  finally { saving.value = false }
}
onMounted(load)
onUnmounted(() => { disposed = true })
</script>

<template>
  <div class="mt-3 space-y-3">
    <p class="text-xs text-gray-500">设置用于后续新任务；继续任务沿用原抓取选项。地址、Cookie 和代理在数据源页面配置。</p>
    <p v-if="loading" class="text-xs text-gray-500">读取设置中…</p>
    <p v-if="error" class="text-xs text-red-400" role="alert">{{ error }} <button v-if="!dirty" class="text-brand" @click="load">重试</button></p>
    <form v-if="!loading && Object.keys(baseline).length" class="space-y-3" @submit.prevent="save">
      <fieldset :disabled="saving" class="grid min-w-0 gap-3 sm:grid-cols-2">
        <div v-for="field in fields" :key="field.key" class="min-w-0">
          <label :for="`crawler-${field.key}`" class="label">{{ field.label }}</label>
          <label v-if="field.type === 'checkbox'" class="flex items-center gap-2 text-sm text-gray-400"><input :id="`crawler-${field.key}`" v-model="form[field.key]" type="checkbox" class="h-4 w-4 accent-emerald-500" />启用</label>
          <div v-else-if="field.type === 'sections'" role="group" :aria-label="field.label" class="flex flex-wrap gap-x-3 gap-y-2">
            <label v-for="(name,key) in sectionChoices(field)" :key="key" class="flex items-center gap-1 text-xs text-gray-400"><input type="checkbox" :checked="selectedSections(field).includes(key)" @change="toggleSection(field,key,$event.target.checked)" />{{ name }}</label>
            <span v-if="!Object.keys(sectionChoices(field)).length" class="text-xs text-gray-500">正在读取板块名称，请稍候或刷新爬虫状态。</span>
          </div>
          <input v-else-if="field.type === 'number'" :id="`crawler-${field.key}`" v-model.number="form[field.key]" type="number" :min="field.min" :max="field.max" required class="input" />
          <input v-else-if="field.type === 'date'" :id="`crawler-${field.key}`" v-model="form[field.key]" class="input" type="date" />
          <template v-else-if="field.job">
            <input :id="`crawler-${field.key}`" v-model="form[field.key]" class="input" placeholder="例如：每天凌晨 4 点" @input="scheduleInput(field)" />
            <p v-if="previews[field.key]?.pending" class="mt-1 text-xs text-gray-500">正在解析…</p>
            <p v-else-if="previews[field.key]?.error" class="mt-1 text-xs text-amber-400">{{ previews[field.key].error }}</p>
            <p v-else-if="previews[field.key]?.text" class="mt-1 text-xs text-emerald-400">将改为：{{ previews[field.key].text }}</p>
            <div class="mt-2 flex flex-wrap gap-1"><button v-for="example in examples" :key="example" type="button" class="rounded bg-gray-800 px-1.5 py-0.5 text-[11px] text-gray-400" @click="form[field.key] = example; scheduleInput(field)">{{ example }}</button></div>
          </template>
          <input v-else :id="`crawler-${field.key}`" v-model="form[field.key]" class="input" :required="!field.job" />
          <p v-if="field.hint" class="mt-1 text-[11px] text-gray-600">{{ field.hint }}</p>
        </div>
      </fieldset>
      <div class="flex flex-wrap items-center gap-3"><button class="btn-primary text-xs" :disabled="saving || !dirty">{{ saving ? '保存中…' : '保存抓取设置' }}</button><RouterLink class="text-xs text-brand" :to="{ name: 'Resources' }">查看资源与爬虫进度 ↗</RouterLink></div>
    </form>
  </div>
</template>
