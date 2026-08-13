<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { listCron, runTask, translateTitles } from '@/api'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'

const toast = useToast()
const jobs = ref([])
const loading = ref(false)
const running = ref('')
let timer = null

// 任务说明，后端只返回 id/name，这里补充用途
const DESCRIPTIONS = {
  run_codes_task: '把已订阅但未下载的番号推送到下载器',
  run_actors: '将订阅演员的新作品加入订阅队列',
  sub_rank: '抓取排行榜并自动订阅',
  sync_hot: '同步热门榜单到本地库',
  sync_brands: '抓取厂牌官网新片',
  sync_actors: '补全演员头像等信息',
  sync_news: '同步最想看榜单',
  fill_empty_banner: '补全缺少详情与封面的番号',
  pt_wait: '同步下载器任务状态',
  translate_titles: '翻译缺中文标题的番号',
  transfer_seeds: '把 qB 已下载完的种子转给 Transmission 做种',
}

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

onMounted(() => {
  load()
  // 下次执行时间会随时间推移，定期刷新
  timer = setInterval(load, 60000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap gap-2">
      <button class="btn-ghost px-3 py-1.5 text-xs" @click="load">刷新</button>
      <button class="btn-ghost px-3 py-1.5 text-xs" @click="doTranslate">立即翻译标题</button>
    </div>

    <LoadingBlock v-if="loading && !jobs.length" :rows="4" />

    <div v-else-if="jobs.length" class="space-y-2">
      <div
        v-for="job in jobs"
        :key="job.id"
        class="card flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between"
      >
        <div class="min-w-0">
          <p class="text-sm font-medium text-gray-200">{{ job.name }}</p>
          <p class="mt-0.5 text-xs text-gray-500">
            {{ DESCRIPTIONS[job.id] || job.id }}
          </p>
          <p v-if="job.next_run" class="mt-1 text-[11px] tabular-nums text-gray-600">
            下次执行 {{ job.next_run }}
          </p>
        </div>
        <button
          class="btn-primary shrink-0 px-3 py-1.5 text-xs"
          :disabled="running === job.id"
          @click="trigger(job.id)"
        >
          {{ running === job.id ? '触发中…' : '立即执行' }}
        </button>
      </div>
    </div>

    <EmptyState v-else text="没有已注册的任务" hint="调度器可能未正常启动" />
  </div>
</template>
