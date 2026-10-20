<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { getMihomo, saveMihomo, mihomoAction, testMihomo, listDataSources } from '@/api'
import { useToast } from '@/composables/useToast'

const toast = useToast()
const state = ref(null)
const form = reactive({ enabled: false, subscription: '', selected: '', trawl_host: 'cinefold', automatic: true, source_nodes: {}, groups: {}, source_groups: {} })
const groupChoices = computed(() => ({ ...form.groups, ...(state.value?.default_groups || {}) }))
const sources = ref([])
const testSource = ref('')
const busy = ref(false)
const error = ref('')
const target = ref('https://www.example.com/')
const testResult = ref('')
let timer
let disposed = false
let wasRunning = false

async function load(initial = false) {
  try {
    const data = await getMihomo()
    if (disposed) return
    state.value = data
    error.value = ''
    if (initial || wasRunning && !data.task?.running) {
      Object.assign(form, {
        enabled: data.enabled, subscription: data.subscription,
        selected: data.selected, trawl_host: data.trawl_host,
        automatic: data.automatic ?? true, source_nodes: { ...data.source_nodes },
        groups: structuredClone(data.groups || {}), source_groups: { ...data.source_groups },
      })
    }
    wasRunning = !!data.task?.running
  } catch (err) {
    if (!disposed) error.value = err.message
  }
}

async function run(action) {
  busy.value = true
  error.value = ''
  try {
    if (action === 'save') await saveMihomo({ ...form })
    else await mihomoAction(action)
    wasRunning = true
    await load()
    toast.success('任务已启动，可在这里查看结果')
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
  }
}

async function test(viaTrawl) {
  busy.value = true
  testResult.value = '正在连接目标站…'
  try {
    const data = await testMihomo({ url: target.value, via_trawl: viaTrawl, source_key: testSource.value })
    testResult.value = `${viaTrawl ? 'TRAWL' : '本机代理'}：${data.message}`
  } catch (err) {
    testResult.value = err.message
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await load(true)
  try { sources.value = [...((await listDataSources())?.items || []), ...(state.value?.brand_sources || [])] } catch (err) { error.value = err.message }
  if (!disposed) timer = setInterval(() => load(), 2000)
})
onUnmounted(() => { disposed = true; clearInterval(timer) })
</script>

<template>
  <section class="card space-y-4" aria-label="内置 Mihomo 代理">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h3 class="text-sm font-medium">内置 Mihomo 代理</h3>
      <span class="text-xs" :class="state?.running ? 'text-brand' : 'text-gray-500'">
        {{ state?.running ? '运行中' : state?.installed ? '已停止' : '未安装内核' }}
        {{ state?.version || '' }}
      </span>
    </div>
    <p class="text-xs text-gray-500">在 cinefold 内运行，无需额外容器。按数据源选择节点并传递给 TRAWL；自动模式按实际访问结果换节点，成功节点优先复用。</p>
    <p v-if="error" class="text-xs text-red-400" role="alert">{{ error }}</p>
    <fieldset :disabled="!state || busy || !!state?.task?.running" class="space-y-4 disabled:opacity-60">
      <label class="flex items-center gap-2 text-sm">
        <input v-model="form.enabled" type="checkbox" class="accent-emerald-500" />启用内置代理
      </label>
      <label class="flex items-center gap-2 text-sm">
        <input v-model="form.automatic" type="checkbox" class="accent-emerald-500" />自动选择可用节点
      </label>
      <p class="text-xs text-gray-500">自动模式最多尝试 3 个节点，成功结果按站点保留 10 分钟，失败节点避开 2 分钟；404 不换节点。指定节点作为该数据源的优先节点，关闭自动模式后固定使用。</p>
      <div>
        <label for="mihomo-subscription" class="label">Clash/Mihomo 订阅地址</label>
        <input id="mihomo-subscription" v-model="form.subscription" type="password" autocomplete="off" class="input" placeholder="https://…" />
        <p class="mt-1 text-xs text-gray-500">支持 YAML 主订阅、HTTP/内联子订阅，最多合并 1024 个节点。只导入节点，自建代理组与分流策略。首次启用会自动安装内核并拉取节点。</p>
      </div>
      <p v-if="state?.nodes?.length" class="text-xs text-gray-400">已导入 {{ state.nodes.length }} 个节点；当前并行请求 {{ state.active_requests || 0 }} 个。并行请求独占空闲节点，每节点每 10 秒最多启动 2 个请求；TRAWL 最多同时处理 2 个请求。</p>
      <div class="grid gap-4 sm:grid-cols-2">
        <div v-if="!form.automatic">
          <label for="mihomo-node" class="label">默认优先节点</label>
          <select id="mihomo-node" v-model="form.selected" class="input">
            <option value="">{{ state?.nodes?.length ? '请选择节点' : '请先更新订阅' }}</option>
            <option v-for="node in state?.nodes || []" :key="node.name" :value="node.name">{{ node.name }}</option>
          </select>
        </div>
        <div>
          <label for="mihomo-host" class="label">TRAWL 访问 cinefold 的主机名</label>
          <input id="mihomo-host" v-model="form.trawl_host" class="input" placeholder="cinefold" />
          <p class="mt-1 text-xs text-gray-500">同一 Docker 网络填写服务名或容器名，无需填写端口。</p>
        </div>
      </div>
      <details class="space-y-3 rounded border border-gray-800 p-3">
        <summary class="cursor-pointer text-sm text-gray-300">默认规则自动分组</summary>
        <p class="text-xs text-gray-500">不采用订阅自带的代理组。按节点名称中的地区、国家代码与旗帜自动归类，订阅更新后自动重建成员，数据源绑定的组名保持不变。</p>
        <div class="grid gap-2 sm:grid-cols-2">
          <p v-for="(members, name) in groupChoices" :key="name" class="rounded border border-gray-800 p-2 text-xs text-gray-400">{{ name }}：{{ members.length }} 个节点</p>
        </div>
      </details>
      <details class="space-y-3 rounded border border-gray-800 p-3">
        <summary class="cursor-pointer text-sm text-gray-300">数据源默认分组与覆盖</summary>
        <p class="text-xs text-gray-500">默认规则优先让官方日本站使用日本节点，其余自动选择；订阅没有日本节点时默认使用全部候选。手动指定组后只在该组内切换。</p>
        <div class="max-h-64 space-y-3 overflow-auto overscroll-contain">
          <div v-for="source in sources" :key="source.key" class="grid items-center gap-2 sm:grid-cols-2">
            <label v-if="!form.automatic" :for="`mihomo-source-${source.key}`" class="text-xs text-gray-400">{{ source.name || source.key }}</label>
            <select v-if="!form.automatic" :id="`mihomo-source-${source.key}`" v-model="form.source_nodes[source.key]" class="input text-xs">
              <option :value="undefined">沿用默认节点</option>
              <option v-if="form.source_nodes[source.key] && !state?.nodes?.some(node => node.name === form.source_nodes[source.key])" :value="form.source_nodes[source.key]">已失效：{{ form.source_nodes[source.key] }}</option>
              <option v-for="node in state?.nodes || []" :key="node.name" :value="node.name">{{ node.name }}</option>
            </select>
            <label :for="`mihomo-source-group-${source.key}`" class="text-xs text-gray-500">{{ source.name || source.key }} 使用代理组</label>
            <select :id="`mihomo-source-group-${source.key}`" v-model="form.source_groups[source.key]" class="input text-xs">
              <option :value="undefined">默认规则（{{ state?.source_defaults?.[source.key] || '自动选择' }}）</option>
              <option v-for="(members, name) in groupChoices" :key="name" :value="name">{{ name }}（{{ members.length }}）</option>
            </select>
          </div>
        </div>
      </details>
      <div class="flex flex-wrap gap-2">
        <button class="btn-primary text-xs" @click="run('save')">保存并应用内置代理</button>
        <button class="btn-ghost text-xs" @click="run('refresh')">更新订阅</button>
        <button class="btn-ghost text-xs" @click="run('check')">检查内核更新</button>
        <button class="btn-ghost text-xs" @click="run('install')">{{ state?.installed ? '在线更新内核' : '在线安装内核' }}</button>
      </div>
      <p v-if="state?.latest" class="text-xs text-gray-400">官方稳定版：{{ state.latest }}</p>
      <div class="space-y-2 border-t border-gray-800 pt-3">
        <label for="mihomo-target" class="label">连通性测试地址</label>
        <label for="mihomo-test-source" class="label">测试使用的数据源规则</label>
        <select id="mihomo-test-source" v-model="testSource" class="input" @change="target = sources.find(source => source.key === testSource)?.host || 'https://www.example.com/'">
          <option value="">默认规则</option>
          <option v-for="source in sources" :key="source.key" :value="source.key">{{ source.name || source.key }}</option>
        </select>
        <input id="mihomo-target" v-model="target" class="input" />
        <div class="flex flex-wrap gap-2">
          <button class="btn-ghost text-xs" :disabled="!state?.running || !state?.enabled" @click="test(false)">测试本机代理</button>
          <button class="btn-ghost text-xs" :disabled="!state?.running || !state?.enabled" @click="test(true)">测试 TRAWL 代理</button>
        </div>
      </div>
    </fieldset>
    <p v-if="state?.task?.message" class="text-xs" :class="['failed', 'interrupted', 'partial'].includes(state.task.stage) ? 'text-amber-400' : 'text-gray-400'" role="status">
      {{ state.task.running ? '处理中：' : '' }}{{ state.task.message }}
      <span v-if="state.task.downloaded_bytes">（已下载 {{ (state.task.downloaded_bytes / 1048576).toFixed(1) }} MB）</span>
    </p>
    <p v-if="testResult" class="text-xs text-gray-400" role="status">{{ testResult }}</p>
    <div v-if="state?.providers?.length" class="max-h-40 space-y-1 overflow-auto text-xs text-gray-500">
      <p v-for="(provider, index) in state.providers" :key="index" :class="provider.status === 'ok' ? '' : 'text-amber-400'">{{ provider.name }}：{{ provider.count }} 个节点，{{ provider.status === 'ok' ? '已更新' : provider.status === 'cached' ? '拉取失败，保留缓存' : '拉取失败' }}</p>
    </div>
    <div v-if="state?.routes?.length" class="max-h-32 overflow-auto text-xs text-gray-500">
      <p v-for="route in state.routes" :key="route.host">{{ route.host }} → {{ route.node }}</p>
    </div>
  </section>
</template>
