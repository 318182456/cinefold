<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import http, { confirmAgentAction, cancelAgentAction } from '@/api'
import { useToast } from '@/composables/useToast'
import ModalDialog from '@/components/ModalDialog.vue'
const toast = useToast()
const proposals = ref([]), busy = ref(false)
const cloudOpen = ref(false), cloudMagnet = ref(''), cloudPath = ref('')
async function submitCloud() {
  busy.value = true
  try {
    await http.post('/cloudnas/offline', { magnet: cloudMagnet.value, save_path: cloudPath.value })
    toast.success('CloudDrive 已接收离线任务'); cloudOpen.value = false
  } catch (e) { toast.error(e.message) } finally { busy.value = false }
}
const endpoint = `${window.location.origin}/api/v1/mcp`
const config = JSON.stringify({ mcpServers: { cinefold: { type: 'http', url: endpoint, headers: { Authorization: 'Bearer <账户页的长期 token>' } } } }, null, 2)
let timer
async function load() { try { proposals.value = await http.get('/agent/proposals') } catch (e) { toast.error(e.message) } }
async function act(row, confirm) { if (confirm && !window.confirm(`确认「${row.label}」，共 ${row.targets.length} 项？`)) return; busy.value = true; try { await (confirm ? confirmAgentAction(row.id) : cancelAgentAction(row.id)); await load(); toast.success(confirm ? '已执行' : '已取消') } catch (e) { toast.error(e.message); await load() } finally { busy.value = false } }
onMounted(() => { load(); timer = setInterval(load, 10000) })
onUnmounted(() => clearInterval(timer))
</script>
<template>
  <div class="space-y-5"><div><h1 class="text-xl font-semibold">工具接入</h1><p class="mt-1 text-sm text-gray-500">将支持 MCP 的客户端连接到 cinefold，查询收藏、资源和任务，并提出订阅操作。</p></div>
    <div class="card space-y-3"><h2 class="font-medium">MCP 连接</h2><p class="break-all rounded-lg bg-gray-950 p-3 text-sm text-brand">{{ endpoint }}</p><p class="text-sm text-gray-400">使用账户页中的长期 token，通过 Authorization: Bearer 传入。支持 Streamable HTTP，客户端配置示例：</p><pre class="overflow-x-auto rounded-lg bg-gray-950 p-3 text-xs text-gray-400">{{ config }}</pre><RouterLink :to="{ name: 'Profile' }" class="btn-ghost">打开账户页</RouterLink></div>
    <div class="card space-y-3"><div class="flex justify-between"><h2 class="font-medium">待确认操作</h2><button class="text-sm text-brand" @click="load">刷新</button></div><p v-if="!proposals.length" class="text-sm text-gray-500">暂无待确认操作。提案 5 分钟后过期。</p>
      <div v-for="row in proposals" :key="row.id" class="space-y-3 rounded-lg border p-3" :class="row.destructive ? 'border-red-900' : 'border-gray-800'"><p :class="row.destructive ? 'text-red-400' : 'text-gray-300'">{{ row.label }} · {{ row.targets.length }} 项</p><p v-for="target in row.targets" :key="target.hash || target.code || target.actor" class="break-all text-xs text-gray-400">{{ target.name }} {{ target.progress_percent != null ? `${target.progress_percent}%` : '' }}</p><div class="flex gap-2"><button :class="row.destructive ? 'btn-danger' : 'btn-primary'" :disabled="busy" @click="act(row, true)">确认{{ row.label }}</button><button class="btn-ghost" :disabled="busy" @click="act(row, false)">取消</button></div></div>
    </div>
    <div class="card space-y-3">
      <h2 class="font-medium">CloudDrive 离线下载</h2>
      <p class="text-sm text-gray-500">使用设置页的 CloudDrive 账号提交磁链。保存目录需预先存在，任务进度在 CloudDrive 中查看。</p>
      <button class="btn-ghost" @click="cloudOpen = true">提交离线任务</button>
    </div>
    <ModalDialog v-model:open="cloudOpen" title="CloudDrive 离线下载">
      <form class="space-y-4" @submit.prevent="submitCloud">
        <label><span class="label">磁链</span><textarea v-model="cloudMagnet" class="input" required rows="3" /></label>
        <label><span class="label">保存目录（留空使用设置值）</span><input v-model="cloudPath" class="input" placeholder="/115open"></label>
        <button class="btn-primary" :disabled="busy">提交</button>
      </form>
    </ModalDialog>
  </div>
</template>
