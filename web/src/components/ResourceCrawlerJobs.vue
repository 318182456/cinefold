<script setup>
import { onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useVisibleTimers } from '@/composables/useVisibleTimers'
import { useToast } from '@/composables/useToast'
import http, { updateDataSource } from '@/api'

const props=defineProps({settingsVersion:{type:Number,default:0}})
const emit=defineEmits(['running','sources'])
const toast=useToast(), sources=ref([]), drafts=reactive({})
const busy=ref(''), jobError=ref(''), baseline={}
const phases={idle:'未运行',queued:'排队中',running:'抓取中',paused:'已暂停',interrupted:'已中断',failed:'失败',finished:'完成'}
const date=value=>new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})
const {setTimeout,clearTimeout}=useVisibleTimers()
let timer, disposed=false, revision=0
async function loadJobs() {
  clearTimeout(timer)
  const request=++revision
  try {
    const result=await http.get('/resource-crawler')
    if(disposed||request!==revision)return
    sources.value=result.sources;jobError.value='';emit('running',result.sources.some(item=>item.job.running));emit('sources',result.sources)
    for(const item of result.sources) {
      if(!drafts[item.key] || (!item.job.running && JSON.stringify(drafts[item.key])===baseline[item.key])) {
        const options=item.job.running ? item.job.options || item.defaults : item.defaults
        drafts[item.key]={...options,sections:[...options.sections]}
        baseline[item.key]=JSON.stringify(drafts[item.key])
      }
    }
  }catch(e){if(!disposed&&request===revision)jobError.value=e.message}
  finally{if(!disposed&&request===revision)timer=setTimeout(loadJobs,sources.value.some(item=>item.job.running)?2500:30000)}
}
async function run(item,resume=false) {
  busy.value=item.key
  try { await http.post('/resource-crawler',{source:item.key,resume,...(resume?{}:drafts[item.key])});await loadJobs() }
  catch(e){toast.error(e.message)}finally{busy.value=''}
}
async function stop(item) {
  busy.value=item.key
  try{await http.post(`/resource-crawler/${item.key}/stop`);toast.success('已请求暂停，当前请求结束后生效');await loadJobs()}
  catch(e){toast.error(e.message)}finally{busy.value=''}
}
async function toggle(item) {
  busy.value=item.key
  try{await updateDataSource(item.key,{enabled:!item.enabled});await loadJobs()}catch(e){toast.error(e.message)}finally{busy.value=''}
}
watch(()=>props.settingsVersion,loadJobs)
onMounted(loadJobs)
onUnmounted(()=>{disposed=true;revision++;clearTimeout(timer)})
</script>
<template>
  <div class="mt-3 space-y-2 border-t border-gray-800 pt-3">
    <p v-if="jobError" class="text-xs text-red-400">{{jobError}}</p>
    <div class="grid gap-3 lg:grid-cols-2">
      <section v-for="item in sources" :key="item.key" class="card space-y-2">
        <div class="flex flex-wrap items-center gap-2"><h2 class="text-sm font-medium">{{item.name}}</h2><span class="text-xs text-gray-500">{{phases[item.job.phase]||item.job.phase}}</span><button class="btn-ghost ml-auto text-xs" :disabled="item.job.running||busy===item.key" @click="toggle(item)">{{item.enabled?'停用':'启用'}}</button></div>
        <details v-if="drafts[item.key]" class="text-xs text-gray-400">
          <summary class="cursor-pointer">新任务抓取设置 · {{drafts[item.key].sections.length}} 个板块 · 每板块 {{drafts[item.key].pages}} 页</summary>
          <div class="mt-2 flex flex-wrap gap-3"><label v-for="(name,key) in item.sections" :key="key" class="flex items-center gap-1"><input v-model="drafts[item.key].sections" type="checkbox" :value="key" :disabled="item.job.running" />{{name}}</label></div>
          <div class="mt-2 flex flex-wrap gap-3"><label>每板块页数 <input v-model.number="drafts[item.key].pages" class="input w-20" type="number" min="1" max="100" :disabled="item.job.running" /></label><label>每轮帖子上限 <input v-model.number="drafts[item.key].limit" class="input w-24" type="number" min="1" max="1000" :disabled="item.job.running" /></label></div>
        </details>
        <div class="flex flex-wrap gap-2"><button class="btn-primary text-xs" :disabled="!item.enabled||item.job.running||busy===item.key" @click="run(item)">开始抓取</button><button v-if="item.job.running" class="btn-ghost text-xs" :disabled="busy===item.key" @click="stop(item)">暂停</button><button v-if="['paused','failed','interrupted'].includes(item.job.phase)" class="btn-ghost text-xs" :disabled="!item.enabled||busy===item.key" @click="run(item,true)">继续任务</button></div>
        <p v-if="item.job.run_id" class="text-xs text-gray-400">已尝试 {{item.job.done||0}} · 新增 {{item.job.saved||0}} · 失败 {{item.job.failed||0}} · 跳过 {{item.job.skipped||0}}</p>
        <p v-if="item.job.message" class="text-xs text-gray-500">{{item.job.message}}</p>
        <p v-if="item.job.error" class="break-all text-xs text-red-400">{{item.job.error}}</p>
        <p v-if="item.job.updated_at" class="text-[11px] text-gray-600">最近更新 {{date(item.job.updated_at)}}</p>
        <details v-if="item.job.errors?.length" class="text-xs text-amber-400"><summary class="cursor-pointer">失败明细（冷却后补抓）</summary><p v-for="(entry,index) in item.job.errors" :key="index">{{entry}}</p></details>
        <details v-if="item.job.history?.length" class="text-xs text-gray-500"><summary class="cursor-pointer">最近任务</summary><p v-for="job in [...item.job.history].reverse()" :key="job.run_id">{{date(job.started_at)}} · {{phases[job.phase]||job.phase}} · 新增 {{job.saved||0}} · 失败 {{job.failed||0}}</p></details>
      </section>
    </div>
    <div class="flex flex-wrap gap-3 text-xs"><button class="text-brand" @click="loadJobs">刷新爬虫状态</button><RouterLink class="text-brand" :to="{name:'Resources'}">查看已抓取资源 ↗</RouterLink></div>
  </div>
</template>
