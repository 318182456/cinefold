<script setup>
import PagePanel from '@/components/PagePanel.vue'
import { onMounted, onUnmounted, ref } from 'vue'
import http from '@/api'
import { useToast } from '@/composables/useToast'
import PlaybackCalendar from '@/components/PlaybackCalendar.vue'
const toast = useToast()
const stats = ref(null), health = ref({ running: false }), year = ref(new Date().getFullYear()), busy = ref(false)
let timer, disposed = false
const gb = n => `${((n || 0) / 1073741824).toFixed(2)} GB`
async function load() { try { stats.value = await http.get('/library/stats', { params: { year: year.value } }) } catch (e) { toast.error(e.message) } }
async function poll() { try { const data = await http.get('/library/health/status'); if (disposed) return; health.value = data; if (health.value.running) timer = setTimeout(poll, 2000) } catch (e) { if (!disposed) toast.error(e.message) } }
async function scan(hashes = false) { busy.value = true; try { clearTimeout(timer); health.value = await http.post('/library/health/scan', { hashes }); await poll() } catch (e) { toast.error(e.message) } finally { busy.value = false } }
onMounted(() => { load(); poll() })
onUnmounted(() => { disposed = true; clearTimeout(timer) })
</script>
<template>
  <PagePanel :collapse-header="false" content-class="space-y-6">
    <template #header>

    <div><h1 class="text-xl font-semibold">收藏分析与健康检查</h1><p class="mt-1 text-sm text-gray-500">了解收藏分布，检查缺失文件、多版本和内容重复。</p></div>
    </template>

    <template v-if="stats">
      <div class="grid grid-cols-2 gap-3 lg:grid-cols-4"><RouterLink :to="{ name: 'Library', query: { ...query, include_skipped: 'true' } }" :title="`查看${label}对应影片`" v-for="[label, value, query] in [['库内影片',stats.total,{}],['已看完',stats.watched,{watched:'true'}],['累计播放',stats.plays,{sort:'plays'}],['收藏',stats.favorites,{favorite:'true'}]]" :key="label" class="card transition hover:border-brand/60 focus-visible:outline focus-visible:outline-brand"><p class="text-sm text-gray-500">{{ label }}</p><p class="mt-2 text-2xl font-semibold">{{ value }}</p></RouterLink></div>
      <div class="grid gap-4 lg:grid-cols-3">
        <div v-for="[label, rows, filter] in [['演员分布',stats.actors,'actor'],['标签分布',stats.genres,'genre'],['发行商分布',stats.publishers,'publisher']]" :key="label" class="card space-y-3">
          <h2 class="font-medium">{{ label }}</h2>
          <RouterLink v-for="row in rows.slice(0,10)" :key="row.name"
            :to="{ name: 'Library', query: { [filter]: row.name } }"
            class="group block rounded-lg p-2 -mx-2 transition-colors hover:bg-gray-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand"
            :title="`查看「${row.name}」的库内作品`">
            <div class="mb-1 flex justify-between text-xs text-gray-400 group-hover:text-brand">
              <span class="truncate pr-3">{{ row.name }}</span><span>{{ row.count }} 部 ↗</span>
            </div>
            <div class="h-1.5 rounded bg-gray-800"><div class="h-full rounded bg-brand/70" :style="{ width: `${row.count / (rows[0]?.count || 1) * 100}%` }" /></div>
          </RouterLink>
          <p v-if="!rows.length" class="text-sm text-gray-500">暂无资料</p>
        </div>
      </div>
      <div class="card space-y-3"><div class="flex items-center gap-3"><h2 class="font-medium">年度观看回顾</h2><input v-model.number="year" class="input !w-28" type="number" min="1900" max="2200" @change="load"></div><p class="text-xs text-gray-500">{{ stats.watch_note }}</p><div class="flex flex-wrap gap-3"><RouterLink :to="{ name: 'Library', query: { last_played_month: month, include_skipped: 'true' } }" :title="`查看 ${month} 最后观看的影片`" v-for="(count, month) in stats.last_watched_months" :key="month" class="rounded-lg bg-gray-800 px-4 py-3 text-center"><p class="text-xs text-gray-500">{{ month }}</p><p class="mt-1">{{ count }} 部</p></RouterLink><p v-if="!Object.keys(stats.last_watched_months).length" class="text-sm text-gray-500">该年暂无观看数据，请先在媒体库页面同步 Emby。</p></div></div>
    </template>
    <PlaybackCalendar />
    <div class="card space-y-4"><div class="flex flex-wrap items-center justify-between gap-3"><h2 class="font-medium">媒体健康检查</h2><div class="flex gap-2"><button class="btn-ghost" :disabled="busy || health.running" @click="scan(false)">快速检查</button><button class="btn-primary" :disabled="busy || health.running" @click="scan(true)">完整内容查重</button></div></div><p class="text-xs text-gray-500">完整查重对同体积文件读取全部内容并计算 SHA-256，大库可能耗时较长。相同硬链接按一份文件计算。检查不会删除文件。</p><p v-if="health.running" class="text-sm text-brand">检查中：{{ health.done }} / {{ health.total }}，文件枚举完成后继续计算内容指纹…</p><p v-if="health.error" class="text-sm text-red-400">{{ health.error }}</p>
      <template v-if="health.result"><div class="flex flex-wrap gap-5 text-sm text-gray-400"><span>文件引用 {{ health.result.links }}</span><span>不同文件 {{ health.result.physical_files }}</span><span>文件逻辑大小 {{ gb(health.result.unique_bytes) }}</span><span>重复内容大小 {{ health.result.hashed ? gb(health.result.duplicate_bytes) : '未检查' }}</span></div><p class="text-xs text-gray-500">文件逻辑大小不等于磁盘实际占用；重复内容大小也不代表可直接释放的空间，文件可能仍有其他硬链接或做种引用。</p>
      <div v-for="(group, index) in health.result.exact" :key="group.sha256" class="rounded-lg border border-amber-800/50 p-3 text-sm"><p class="text-amber-400">内容重复组 {{ index + 1 }} · {{ gb(group.size) }}</p><p v-for="f in group.files" :key="f.path" class="mt-2 break-all text-xs text-gray-400">{{ f.code }} · {{ f.path }}</p></div>
      <details v-if="health.result.versions.length"><summary class="cursor-pointer text-sm">同番号多文件（{{ health.result.versions.length }} 组）</summary><div v-for="v in health.result.versions" :key="v.code" class="my-3 rounded-lg bg-gray-800 p-3 text-sm"><p>{{ v.code }} <span class="text-gray-500">{{ v.multipart ? '含分卷标记，请保留完整分卷' : '多版本或多个文件引用，需人工核对' }}</span></p><p v-for="f in v.files" :key="f.path" class="mt-2 break-all text-xs text-gray-400">{{ f.path }} · {{ gb(f.size) }}</p></div></details>
      <div v-for="(issue, index) in health.result.issues" :key="index" class="border-t border-gray-800 pt-3 text-sm"><p>{{ issue.code }} · {{ issue.message }}</p><p class="mt-1 break-all text-xs text-gray-500">{{ issue.path }}</p></div><p v-if="!health.result.issues.length" class="text-sm text-green-400">未发现缺失、空文件或资料缺项。</p><RouterLink :to="{ name: 'MediaLink' }" class="btn-ghost">到硬链接管理预览清理范围</RouterLink></template>
    </div>
  </PagePanel>
</template>
