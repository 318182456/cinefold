<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import PagePanel from '@/components/PagePanel.vue'
import ResourcePostCard from '@/components/ResourcePostCard.vue'
import { useVisibleTimers } from '@/composables/useVisibleTimers'
import http from '@/api'

const sources=ref([]), data=ref({items:[],total:0}), keyword=ref(''), source=ref(''), page=ref(1), error=ref('')
const {setTimeout,clearTimeout}=useVisibleTimers()
let timer, disposed=false, revision=0
async function loadResources() {
  clearTimeout(timer)
  const id=++revision
  try { const result=await http.get('/resources',{params:{keyword:keyword.value.trim(),source:source.value,page:page.value,size:20}}); if(!disposed&&id===revision){data.value=result;error.value=''} }
  catch(e){if(!disposed&&id===revision)error.value=e.message}
  finally{if(!disposed&&id===revision)timer=setTimeout(loadResources,30000)}
}
async function loadSources() {
  try { const result=await http.get('/resource-crawler');if(!disposed)sources.value=result.sources }
  catch(e){if(!disposed)error.value=e.message}
}
function search(){page.value=1;loadResources()}
function paginate(step){page.value+=step;loadResources()}
onMounted(()=>Promise.all([loadSources(),loadResources()]))
onUnmounted(()=>{disposed=true;revision++;clearTimeout(timer)})
</script>

<template>
  <PagePanel content-class="space-y-3" mobile-summary="本地资源 · 搜索与下载">
    <template #header>
      <div class="flex flex-wrap items-center justify-between gap-2"><h1 class="text-lg font-semibold">资源库</h1><div class="flex flex-wrap gap-2"><RouterLink class="btn-ghost" :to="{name:'Task',query:{section:'resources'}}">任务与抓取设置</RouterLink><RouterLink class="btn-ghost" :to="{name:'DataSource'}">地址 / Cookie / 代理</RouterLink></div></div>
      <p class="text-xs text-gray-500">新资源直接入库；列表按需显示封面，展开后查看图片，不批量缓存论坛图片。搜索实时包含本地磁链，影片与演员情报抓取在任务页运行。</p>
    </template>
    <form class="flex flex-wrap gap-2" @submit.prevent="search"><input v-model="keyword" class="input min-w-0 flex-1" placeholder="搜索资源标题或番号" /><select v-model="source" class="input w-32" @change="search"><option value="">全部来源</option><option v-for="item in sources" :key="item.key" :value="item.key">{{item.name}}</option></select><button class="btn-ghost">搜索</button></form>
    <p v-if="error" class="text-sm text-red-400">{{error}}</p>
    <p class="text-xs text-gray-500">{{data.total}} 条资源</p>
    <ResourcePostCard v-for="row in data.items" :key="row.key" :row="row"
      :source-name="sources.find(s=>s.key===row.source)?.name || row.source"
      :section-name="sources.find(s=>s.key===row.source)?.sections?.[row.section] || ''" />
    <p v-if="!data.total&&!error" class="text-sm text-gray-500">暂无资源，可到任务页启用论坛并开始抓取。</p>
    <div v-if="data.total>20" class="flex items-center justify-center gap-3 text-xs"><button class="btn-ghost" :disabled="page<=1" @click="paginate(-1)">上一页</button><span>{{page}} / {{Math.ceil(data.total/20)}}</span><button class="btn-ghost" :disabled="page*20>=data.total" @click="paginate(1)">下一页</button></div>
  </PagePanel>
</template>
