<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import http from '@/api'
import { useToast } from '@/composables/useToast'
import ModalDialog from '@/components/ModalDialog.vue'
const toast = useToast(), feeds = ref([]), items = ref([]), keyword = ref(''), page = ref(1), total = ref(0), status = ref({ running: false }), busy = ref(false)
const open = ref(false), editing = ref(null), form = ref({}), importing = ref(false), importText = ref('')
const cloudOpen = ref(false), cloudMagnet = ref(''), cloudPath = ref('')
let timer, disposed = false
async function perform(fn) { busy.value = true; try { await fn() } catch (e) { toast.error(e.message) } finally { busy.value = false } }
async function load() { const result = await http.get('/resources', { params: { keyword: keyword.value, page: page.value } }); items.value = result.items; total.value = result.total }
async function loadFeeds() { feeds.value = await http.get('/resources/feeds') }
function edit(row = null) { editing.value = row?.id || null; form.value = row ? { ...row, cookie: null } : { name: '', url: '', kind: 'json', enabled: false, cookie: '' }; open.value = true }
async function save() { await perform(async () => { const { name, url, kind, enabled, cookie } = form.value; await (editing.value ? http.put(`/resources/feeds/${editing.value}`, { name, url, kind, enabled, cookie }) : http.post('/resources/feeds', { name, url, kind, enabled, cookie })); open.value = false; await loadFeeds() }) }
async function poll() { try { const data = await http.get('/resources/status'); if (disposed) return; status.value = data; if (status.value.running) timer = setTimeout(poll, 2000); else await load() } catch (e) { if (!disposed) toast.error(e.message) } }
function safeUrl(url) { return /^https?:\/\//i.test(url || '') ? url : '' }
onMounted(() => { perform(async () => { await Promise.all([load(), loadFeeds()]); await poll() }) })
onUnmounted(() => { disposed = true; clearTimeout(timer) })
</script>
<template>
  <div class="space-y-5"><div class="flex flex-wrap items-center justify-between gap-3"><div><h1 class="text-xl font-semibold">资源索引</h1><p class="mt-1 text-sm text-gray-500">从 JSON、RSS 或论坛采集磁链，按番号关联后参与资源搜索。采集不会自动订阅影片。</p></div><div class="flex gap-2"><button class="btn-ghost" @click="importing = true">导入 JSON</button><button class="btn-primary" @click="edit()">添加资源源</button></div></div>
    <div class="card space-y-3"><div v-for="feed in feeds" :key="feed.id" class="flex flex-wrap items-center gap-3 border-b border-gray-800 py-2 last:border-0"><span class="badge" :class="feed.enabled ? 'bg-green-500/10 text-green-400' : 'bg-gray-800 text-gray-500'">{{ feed.enabled ? '启用' : '停用' }}</span><span class="flex-1 text-sm">{{ feed.name }} · {{ feed.kind }}</span><button class="btn-ghost" @click="edit(feed)">编辑</button><button class="btn-ghost" @click="perform(async () => { if (!window.confirm(`删除资源源「${feed.name}」？已采集资源会保留。`)) return; await http.delete(`/resources/feeds/${feed.id}`); await loadFeeds() })">删除</button></div><p v-if="!feeds.length" class="text-sm text-gray-500">尚未配置资源源。论坛模式支持 Discuz 的 thread / viewthread 主题页与版块页。</p><button class="btn-ghost" :disabled="busy || status.running || !feeds.some(f => f.enabled)" @click="perform(async () => { clearTimeout(timer); status = await http.post('/resources/collect'); await poll() })">{{ status.running ? '采集中…' : '立即采集' }}</button><p class="text-xs text-gray-500">启用的资源源每小时自动采集；论坛每轮最多处理当前页 30 个主题。</p><p v-if="status.added" class="text-sm text-green-400">本轮新增 {{ status.added }} 条</p><p v-for="e in status.errors" :key="e" class="text-xs text-red-400">{{ e }}</p></div>
    <form class="flex gap-3" @submit.prevent="page = 1; perform(load)"><input v-model="keyword" class="input flex-1" placeholder="搜索资源标题或番号"><button class="btn-ghost">搜索</button></form>
    <div v-for="item in items" :key="item.hash" class="card space-y-2"><p class="text-sm font-medium">{{ item.title || '未命名资源' }}</p><p class="text-xs text-gray-500">{{ item.source }} · {{ item.size_mb ? `${(item.size_mb / 1024).toFixed(2)} GB` : '体积未知' }}</p><div class="flex flex-wrap gap-3"><a :href="item.magnet" class="text-xs text-brand">打开磁链 ↗</a><a v-if="safeUrl(item.detail_url)" :href="safeUrl(item.detail_url)" target="_blank" rel="noopener noreferrer" class="text-xs text-gray-400">来源页 ↗</a></div></div><p v-if="!items.length" class="py-8 text-center text-gray-500">暂无资源</p><div class="flex justify-center gap-3"><button class="btn-ghost" :disabled="page <= 1 || busy" @click="page--; perform(load)">上一页</button><span class="self-center text-sm text-gray-500">{{ page }} / {{ Math.max(1, Math.ceil(total / 30)) }}</span><button class="btn-ghost" :disabled="page * 30 >= total || busy" @click="page++; perform(load)">下一页</button></div>
    <ModalDialog v-model:open="open" title="资源源"><form class="space-y-4" @submit.prevent="save"><label><span class="label">名称</span><input v-model="form.name" class="input" required maxlength="128"></label><label><span class="label">地址</span><input v-model="form.url" class="input" type="url" required></label><label><span class="label">类型</span><select v-model="form.kind" class="input"><option value="json">JSON 资源列表</option><option value="rss">RSS 订阅</option><option value="forum">论坛版块或主题页</option></select></label><label><span class="label">Cookie（编辑时留空保留原值）</span><input :value="form.cookie || ''" class="input" type="password" autocomplete="off" @input="form.cookie = $event.target.value"><button v-if="editing && form.has_cookie" type="button" class="mt-1 text-xs text-gray-500" @click="form.cookie = ''">清除已保存 Cookie</button></label><label class="flex gap-2 text-sm"><input v-model="form.enabled" type="checkbox">启用定时采集</label><button class="btn-primary" :disabled="busy">保存</button></form></ModalDialog>
    <ModalDialog v-model:open="importing" title="导入资源 JSON" size="xl"><div class="space-y-4"><p class="text-sm text-gray-500">格式为数组，每项包含 title、magnet 或 download_url，可选 size_mb、detail_url。最多 2000 项。</p><textarea v-model="importText" class="input font-mono" rows="10" placeholder='[{"title":"SONE-001", "magnet":"magnet:?xt=urn:btih:..."}]' /><button class="btn-primary" :disabled="busy" @click="perform(async () => { const result = await http.post('/resources/import', { items: JSON.parse(importText) }); toast.success(`新增 ${result.added} 条`); importing = false; await load() })">导入</button></div></ModalDialog>
    <div class="card space-y-3">
      <h2 class="font-medium">CloudDrive 离线下载</h2>
      <p class="text-sm text-gray-500">使用设置页的 CloudDrive 账号提交磁链。保存目录需预先存在；离线任务在 CloudDrive 中查看。</p>
      <button class="btn-ghost" @click="cloudOpen = true">提交离线任务</button>
    </div>
    <ModalDialog v-model:open="cloudOpen" title="CloudDrive 离线下载">
      <form class="space-y-4" @submit.prevent="perform(async () => { await http.post('/resources/cloudnas/offline', { magnet: cloudMagnet, save_path: cloudPath }); toast.success('CloudDrive 已接收离线任务'); cloudOpen = false })">
        <label><span class="label">磁链</span><textarea v-model="cloudMagnet" class="input" required rows="3" /></label>
        <label><span class="label">保存目录（留空使用设置值）</span><input v-model="cloudPath" class="input" placeholder="/115open"></label>
        <button class="btn-primary" :disabled="busy">提交</button>
      </form>
    </ModalDialog>
  </div>
</template>
