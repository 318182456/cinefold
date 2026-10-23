<script setup>
import MobileSection from '@/components/MobileSection.vue'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import http from '@/api'
import { revealDetails } from '@/utils/statNavigation'
import { useToast } from '@/composables/useToast'
const toast = useToast()
const today = new Date()
const month = ref(`${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}`)
const user = ref(''), day = ref(''), page = ref(1), data = ref(null), busy = ref(false)
let request = 0, timer, disposed = false
const blanks = computed(() => (new Date(`${month.value}-01T12:00:00`).getDay() + 6) % 7)
const duration = seconds => {
  const minutes = Math.floor((seconds || 0) / 60)
  return minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分` : minutes ? `${minutes} 分` : `${Math.round(seconds || 0)} 秒`
}
function time(value) {
  return value ? new Intl.DateTimeFormat('zh-CN', { timeZone: data.value?.timezone || 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) : '—'
}
async function load() {
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(month.value)) return
  const id = ++request
  try {
    const result = await http.get('/playback', { params: { month: month.value, user_id: user.value, day: day.value, page: page.value } })
    if (!disposed && id === request) data.value = result
  } catch (e) { if (!disposed && id === request) toast.error(e.message) }
}
function showMonthDetails() {
  if (day.value) day.value = ''
  else if (page.value !== 1) page.value = 1
  else load()
  revealDetails('playback-records')
}
function showDay(value) { day.value = day.value === value ? '' : value; revealDetails('playback-records') }
async function refresh() {
  busy.value = true
  try { await http.post('/playback/poll'); await load() } catch (e) { toast.error(e.message) } finally { busy.value = false }
}
watch([month, user], () => { day.value = ''; page.value = 1; load() })
watch(day, () => { page.value = 1; load() })
watch(page, load)
onMounted(() => { load(); timer = setInterval(load, 60000) })
onUnmounted(() => { disposed = true; request++; clearInterval(timer) })
</script>

<template>
  <section class="card space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <h2 class="font-medium">Emby 播放日历</h2>
      <div class="flex flex-wrap gap-2">
        <input v-model="month" type="month" min="1900-01" max="2199-12" class="input !w-auto" aria-label="播放月份" />
        <select v-model="user" class="input !w-auto" aria-label="播放用户"><option value="">全部已记录用户</option><option v-for="u in data?.users || []" :key="u.user_id" :value="u.user_id">{{ u.name }}</option></select>
        <button class="btn-ghost" :disabled="busy" @click="refresh">{{ busy ? '读取中…' : '刷新播放' }}</button>
      </div>
    </div>
    <template v-if="data">
      <p v-if="!data.enabled" class="text-sm text-amber-400">播放记录尚未启用。<RouterLink :to="{ name: 'Config' }" class="text-brand">设置 → 媒体服务器 → Emby</RouterLink> 中开启后开始记录。</p>
      <p v-if="data.source_error" class="text-xs text-amber-400">Playback Reporting 读取失败（{{ data.source_error }}），当前显示本地观测记录。</p>
      <MobileSection title="月历与统计" :summary="`${data.plays} 次 · ${duration(data.seconds)}`">
      <p class="text-xs text-gray-500">{{ data.note }} 日历时区：{{ data.timezone }}。</p>
      <div class="grid grid-cols-3 gap-3 text-center"><button type="button" class="rounded-lg bg-gray-800/60 p-3 transition hover:bg-gray-700/70 focus-visible:outline focus-visible:outline-brand" title="查看本月播放明细" @click="showMonthDetails"><p class="text-xs text-gray-500">本月播放</p><p class="mt-1 text-lg">{{ data.plays }} 次</p></button><button type="button" class="rounded-lg bg-gray-800/60 p-3 transition hover:bg-gray-700/70 focus-visible:outline focus-visible:outline-brand" title="查看本月播放明细" @click="showMonthDetails"><p class="text-xs text-gray-500">{{ data.source === 'playback_reporting' ? '本月观看时长（插件）' : '本月观看时长（估算）' }}</p><p class="mt-1 text-lg">{{ duration(data.seconds) }}</p></button><button type="button" class="rounded-lg bg-gray-800/60 p-3 transition hover:bg-gray-700/70 focus-visible:outline focus-visible:outline-brand" title="查看本月播放明细" @click="showMonthDetails"><p class="text-xs text-gray-500">活跃天数</p><p class="mt-1 text-lg">{{ data.active_days }} 天</p></button></div>
      <div class="grid grid-cols-7 gap-1.5 sm:gap-2">
        <span v-for="weekday in ['一', '二', '三', '四', '五', '六', '日']" :key="weekday" class="py-1 text-center text-xs text-gray-500">{{ weekday }}</span>
        <span v-for="n in blanks" :key="`blank-${n}`" />
        <button v-for="cell in data.days" :key="cell.date" class="min-h-16 rounded-lg border p-1.5 text-left transition sm:p-2" :class="[day === cell.date ? 'border-brand ring-1 ring-brand' : 'border-transparent hover:border-gray-600', cell.seconds >= 3600 ? 'bg-emerald-800/70' : cell.plays ? 'bg-emerald-950/70' : 'bg-gray-800/40']" :aria-label="`${cell.date}，${cell.plays} 次播放，${duration(cell.seconds)}`" :aria-pressed="day === cell.date" @click="showDay(cell.date)">
          <span class="text-xs text-gray-300">{{ Number(cell.date.slice(-2)) }}</span>
          <p v-if="cell.plays" class="mt-1 text-[10px] text-emerald-300 sm:text-xs">{{ cell.plays }} 次<span class="hidden sm:inline"> · {{ duration(cell.seconds) }}</span></p>
        </button>
      </div>
      </MobileSection>
      <div id="playback-records" class="flex scroll-mt-4 items-center justify-between"><h3 class="text-sm">{{ day || month }} 播放记录</h3><button v-if="day" class="text-xs text-brand" @click="day = ''">查看整月</button></div>
      <p v-if="!data.records.length" class="text-sm text-gray-500">暂无记录，启用后在 Emby 中播放即可累计。</p>
      <article v-for="row in data.records" :key="row.id" class="flex flex-wrap items-center justify-between gap-2 border-t border-gray-800 pt-3">
        <div class="min-w-0 flex-1"><p class="truncate text-sm">{{ row.title || row.code }} <span v-if="row.active" class="text-xs text-brand">{{ row.paused ? '暂停中' : '播放中' }}</span></p><p class="mt-1 text-xs text-gray-500">{{ time(row.started_at) }} · {{ row.user_name || row.user_id }} · {{ row.device }} {{ row.client }}</p></div>
        <span class="text-xs text-gray-400">本次累计 {{ duration(row.watched_seconds) }}</span>
        <a v-if="row.emby_url" :href="row.emby_url" target="_blank" rel="noopener noreferrer" class="text-xs text-brand">Emby ↗</a>
      </article>
      <div v-if="data.total > data.size" class="flex items-center justify-center gap-4"><button class="btn-ghost" :disabled="page <= 1" @click="page--">上一页</button><span class="text-xs text-gray-500">{{ page }} / {{ Math.ceil(data.total / data.size) }}</span><button class="btn-ghost" :disabled="page * data.size >= data.total" @click="page++">下一页</button></div>
    </template>
  </section>
</template>
