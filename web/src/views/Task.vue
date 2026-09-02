<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { listCron, previewSchedule, runTask, translateTitles, updateSchedule } from '@/api'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'

const toast = useToast()
const jobs = ref([])
const loading = ref(false)
const running = ref('')
let timer = null

// 正在改排班的任务，以及输入框里的说法
const editing = ref('')
const draft = ref('')
const saving = ref(false)
// 输入的实时回读：「每天 04:00」。翻不出来时存错误提示
const preview = ref({ text: '', error: '' })
let previewTimer = null
const inputEl = ref(null)

// 任务说明，后端只返回 id/name，这里补充用途
const DESCRIPTIONS = {
  run_codes_task: '把已订阅但未下载的番号推送到下载器',
  run_actors: '将订阅演员的新作品加入订阅队列',
  sub_rank: '抓取排行榜并自动订阅',
  sub_brands: '抓取厂牌新片并自动订阅',
  sync_hot: '同步热门榜单到本地库',
  sync_brands: '抓取厂牌官网新片',
  sync_actors: '补全演员头像等信息',
  sync_news: '同步最想看榜单',
  fill_empty_banner: '补全缺少详情与封面的番号',
  warm_page_cache: '提前抓好推荐/榜单/厂牌页，打开即命中缓存',
  fill_subtitles: '给媒体库里没字幕的影片补抓',
  fill_reviews: '给媒体库里没影评的影片补生成 AI 影评',
  import_crawler_db: '从外部爬虫库导入番号情报',
  pt_wait: '同步下载器任务状态',
  translate_titles: '翻译缺中文标题的番号',
  cache_photos: '把封面图缓存到本地',
  transfer_seeds: '把 qB 已下载完的种子转给 Transmission 做种',
  sync_watch_dirs: '监控目录全量对账，兜底 inotify 丢失的事件',
  scan_orphans: '扫描下载侧已删、媒体库仍在的关联',
  refresh_link_sizes: '回填媒体关联的体积与字幕状态',
  auto_update: '检查并安装新版本',
}

// 输入框下方的例子。放在界面上而不是等出错再说 —— 用户第一次点开就该
// 知道这里能填什么，而不是先猜一次、被拒一次才学会
const CRON_HINTS = ['每小时', '每 2 小时', '每天凌晨 4 点', '每周一早上 9 点', '工作日中午']
const INTERVAL_HINTS = ['每 5 分钟', '每 30 分钟', '每小时', '每 2 小时']

const hints = computed(() => {
  const job = jobs.value.find((j) => j.id === editing.value)
  return job?.kind === 'interval' ? INTERVAL_HINTS : CRON_HINTS
})

async function load() {
  loading.value = true
  try {
    const data = await listCron()
    jobs.value = data.jobs || []
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

async function trigger(jobId) {
  running.value = jobId
  try {
    await runTask(jobId)
    toast.success('任务已触发，执行情况见日志页')
  } catch (err) {
    toast.error(err.message)
  } finally {
    running.value = ''
  }
}

async function doTranslate() {
  try {
    await translateTitles()
    toast.success('翻译任务已触发')
  } catch (err) {
    toast.error(err.message)
  }
}

function startEdit(job) {
  editing.value = job.id
  // 预填当前说法而不是留空：多数改动是微调（每小时 → 每 2 小时），
  // 有个起点比从头打一遍省事
  draft.value = job.schedule_text || ''
  preview.value = { text: '', error: '' }
  nextTick(() => inputEl.value?.[0]?.select?.() ?? inputEl.value?.select?.())
}

function cancelEdit() {
  editing.value = ''
  draft.value = ''
  preview.value = { text: '', error: '' }
  clearTimeout(previewTimer)
}

// 边打边翻。防抖 400ms —— 规则命中的说法是本地正则，很快；
// 规则没命中才会走到 AI，那一跳有网络往返，不防抖会边打边发一串请求
function onInput() {
  clearTimeout(previewTimer)
  preview.value = { text: '', error: '' }
  const text = draft.value.trim()
  if (!text) return
  previewTimer = setTimeout(async () => {
    const jobId = editing.value
    try {
      const data = await previewSchedule(jobId, text)
      // 等回包的这段时间里用户可能已经切走了，回来的结果就不该再显示
      if (editing.value !== jobId || draft.value.trim() !== text) return
      preview.value = { text: data.schedule_text, error: '' }
    } catch (err) {
      if (editing.value !== jobId || draft.value.trim() !== text) return
      preview.value = { text: '', error: err.message }
    }
  }, 400)
}

async function save(job) {
  const text = draft.value.trim()
  if (!text) return
  saving.value = true
  try {
    const data = await updateSchedule(job.id, text)
    toast.success(`「${job.name}」已改为${data.schedule_text}`)
    cancelEdit()
    await load()
  } catch (err) {
    toast.error(err.message)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  load()
  // 下次执行时间会随时间推移，定期刷新
  timer = setInterval(load, 60000)
})
onUnmounted(() => {
  clearInterval(timer)
  clearTimeout(previewTimer)
})
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap gap-2">
      <button class="btn-ghost px-3 py-1.5 text-xs" @click="load">刷新</button>
      <button class="btn-ghost px-3 py-1.5 text-xs" @click="doTranslate">立即翻译标题</button>
    </div>

    <LoadingBlock v-if="loading && !jobs.length" :rows="4" />

    <div v-else-if="jobs.length" class="space-y-2">
      <div v-for="job in jobs" :key="job.id" class="card">
        <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div class="min-w-0">
            <p class="text-sm font-medium text-gray-200">{{ job.name }}</p>
            <p class="mt-0.5 text-xs text-gray-500">
              {{ DESCRIPTIONS[job.id] || job.id }}
            </p>
            <div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
              <!-- 排班用人话显示，cron 原文收进 title 里，需要时才看 -->
              <span
                class="text-[11px] text-gray-400"
                :title="job.kind === 'cron' ? job.schedule : `${job.schedule} 分钟`"
              >
                {{ job.schedule_text }}
              </span>
              <span v-if="job.next_run" class="text-[11px] tabular-nums text-gray-600">
                下次执行 {{ job.next_run }}
              </span>
            </div>
          </div>

          <div class="flex shrink-0 gap-2">
            <button
              v-if="job.editable && editing !== job.id"
              class="btn-ghost px-3 py-1.5 text-xs"
              @click="startEdit(job)"
            >
              改周期
            </button>
            <button
              class="btn-primary px-3 py-1.5 text-xs"
              :disabled="running === job.id"
              @click="trigger(job.id)"
            >
              {{ running === job.id ? '触发中…' : '立即执行' }}
            </button>
          </div>
        </div>

        <!-- 改周期：直接说人话，不用会 cron -->
        <div v-if="editing === job.id" class="mt-3 border-t border-gray-800 pt-3">
          <div class="flex flex-col gap-2 sm:flex-row">
            <input
              ref="inputEl"
              v-model="draft"
              class="input flex-1 text-sm"
              placeholder="用大白话写，例如：每天凌晨 4 点"
              @input="onInput"
              @keyup.enter="save(job)"
              @keyup.esc="cancelEdit"
            />
            <div class="flex gap-2">
              <button
                class="btn-primary px-3 py-1.5 text-xs"
                :disabled="saving || !draft.trim()"
                @click="save(job)"
              >
                {{ saving ? '保存中…' : '保存' }}
              </button>
              <button class="btn-ghost px-3 py-1.5 text-xs" @click="cancelEdit">取消</button>
            </div>
          </div>

          <!-- 回读：存进去之前先让用户确认理解得对不对 -->
          <p v-if="preview.text" class="mt-2 text-xs text-emerald-400">
            将改为：{{ preview.text }}
          </p>
          <p v-else-if="preview.error" class="mt-2 text-xs text-amber-400">
            {{ preview.error }}
          </p>
          <div v-else class="mt-2 flex flex-wrap items-center gap-1.5">
            <span class="text-[11px] text-gray-600">可以这样写：</span>
            <button
              v-for="h in hints"
              :key="h"
              class="rounded bg-gray-800 px-1.5 py-0.5 text-[11px] text-gray-400 transition-colors hover:bg-gray-700 hover:text-gray-200"
              @click="((draft = h), onInput())"
            >
              {{ h }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <EmptyState v-else text="没有已注册的任务" hint="调度器可能未正常启动" />
  </div>
</template>
