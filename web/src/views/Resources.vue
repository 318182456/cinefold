<script setup>
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import PagePanel from '@/components/PagePanel.vue'
import { useVisibleTimers } from '@/composables/useVisibleTimers'
import { useToast } from '@/composables/useToast'
import http, { updateDataSource } from '@/api'

const toast=useToast(), sources=ref([]), drafts=reactive({}), data=ref({items:[],total:0}), keyword=ref(''), source=ref(''), page=ref(1)
const busy=ref(''), error=ref(''), jobError=ref(''), loaded=ref(false)
const phases={idle:'未运行',queued:'排队中',running:'抓取中',paused:'已暂停',interrupted:'已中断',failed:'失败',finished:'完成'}
const date=value=>new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})
const {setTimeout,clearTimeout}=useVisibleTimers()
let timer, disposed=false, revision=0
async function loadResources() {
  const id=++revision
  try { const result=await http.get('/resources',{params:{keyword:keyword.value.trim(),source:source.value,page:page.value,size:20}}); if(!disposed&&id===revision){data.value=result;error.value=''} }
  catch(e){if(!disposed&&id===revision)error.value=e.message}
}
async function loadJobs() {
  clearTimeout(timer)
  try {
    const result=await http.get('/resource-crawler')
    if(disposed)return
    const changed=loaded.value&&result.sources.some(item=>sources.value.find(old=>old.key===item.key)?.job.saved!==item.job.saved)
    sources.value=result.sources;jobError.value='';loaded.value=true
    for(const item of result.sources) if(!drafts[item.key]) drafts[item.key]={...(item.job.options||item.defaults),sections:[...(item.job.options||item.defaults).sections]}
    if(changed)await loadResources()
  }catch(e){if(!disposed)jobError.value=e.message}
  finally{if(!disposed)timer=setTimeout(loadJobs,sources.value.some(item=>item.job.running)?2500:30000)}
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
async function copy(value){try{await navigator.clipboard.writeText(value);toast.success('链接已复制')}catch{toast.error('无法复制，请手动选择链接')}}
function search(){page.value=1;loadResources()}
function paginate(step){page.value+=step;loadResources()}
onMounted(()=>Promise.all([loadJobs(),loadResources()]))
onUnmounted(()=>{disposed=true;revision++;clearTimeout(timer)})
</script>

<template>
  <PagePanel content-class="space-y-3" mobile-summary="原生抓取 · 无需 article">
    <template #header>
      <div class="flex flex-wrap items-center justify-between gap-2"><h1 class="text-lg font-semibold">资源库与爬虫</h1><RouterLink class="btn-ghost" :to="{name:'DataSource'}">地址 / Cookie / 代理</RouterLink></div>
      <p class="text-xs text-gray-500">新资源直接入库；不下载预览图。搜索实时包含本地磁链，自动下载沿用 BT 开关。影片与演员情报抓取在任务页运行。</p>
    </template>
    <p v-if="jobError" class="text-sm text-red-400">{{jobError}}</p>
    <div class="grid gap-3 lg:grid-cols-2">
      <section v-for="item in sources" :key="item.key" class="card space-y-2">
        <div class="flex flex-wrap items-center gap-2"><h2 class="text-sm font-medium">{{item.name}}</h2><span class="text-xs text-gray-500">{{phases[item.job.phase]||item.job.phase}}</span><button class="btn-ghost ml-auto text-xs" :disabled="item.job.running||busy===item.key" @click="toggle(item)">{{item.enabled?'停用':'启用'}}</button></div>
        <details v-if="drafts[item.key]" class="text-xs text-gray-400">
          <summary class="cursor-pointer">本次抓取设置 · {{drafts[item.key].sections.length}} 个板块 · 每板块 {{drafts[item.key].pages}} 页</summary>
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
    <form class="flex flex-wrap gap-2" @submit.prevent="search"><input v-model="keyword" class="input min-w-0 flex-1" placeholder="搜索资源标题或番号" /><select v-model="source" class="input w-32" @change="search"><option value="">全部来源</option><option v-for="item in sources" :key="item.key" :value="item.key">{{item.name}}</option></select><button class="btn-ghost">搜索</button></form>
    <p v-if="error" class="text-sm text-red-400">{{error}}</p>
    <p class="text-xs text-gray-500">{{data.total}} 条资源</p>
    <article v-for="row in data.items" :key="row.key" class="card space-y-2">
      <p class="break-words text-sm font-medium">{{row.title}}</p>
      <div class="flex flex-wrap gap-3 text-xs text-gray-500"><RouterLink v-if="row.code" class="text-brand" :to="{name:'Search',query:{keyword:row.code}}">{{row.code}} · 搜索下载</RouterLink><span v-else>未确认单一番号，仅供手动查看</span><span>{{sources.find(s=>s.key===row.source)?.name||row.source}} · {{row.published||'日期未知'}}</span><span v-if="row.size_mb">{{(row.size_mb/1024).toFixed(2)}} GiB</span><a v-if="/^https?:/.test(row.detail_url)" :href="row.detail_url" target="_blank" rel="noreferrer" class="text-brand">查看原帖 ↗</a></div>
      <details class="text-xs text-gray-400"><summary class="cursor-pointer">磁链 {{row.magnets.length}} · ED2K {{row.ed2k.length}}</summary><div v-for="link in [...row.magnets,...row.ed2k].slice(0,100)" :key="link" class="mt-2 flex items-start gap-2"><code class="min-w-0 flex-1 break-all">{{link}}</code><button class="btn-ghost shrink-0 text-xs" @click="copy(link)">复制</button></div></details>
    </article>
    <p v-if="!data.total&&!error" class="text-sm text-gray-500">暂无资源，启用数据源后开始抓取。旧资源库不会导入。</p>
    <div v-if="data.total>20" class="flex items-center justify-center gap-3 text-xs"><button class="btn-ghost" :disabled="page<=1" @click="paginate(-1)">上一页</button><span>{{page}} / {{Math.ceil(data.total/20)}}</span><button class="btn-ghost" :disabled="page*20>=data.total" @click="paginate(1)">下一页</button></div>
  </PagePanel>
</template>
