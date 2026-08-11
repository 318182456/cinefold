<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getBrands, getBrandCodes } from '@/api'
import { useToast } from '@/composables/useToast'
import CodeCard from '@/components/CodeCard.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'

const toast = useToast()

const brands = ref([])
const publishers = ref([])
const active = ref('')
const items = ref([])
const loading = ref(false)
const loadingList = ref(false)
const error = ref('')

// 只看预定发布 / 只看已发布 / 全部
const filter = ref('all')
const keyword = ref('')
// '' 全部 / 'no' 未订阅 / 'yes' 已订阅
const subFilter = ref('')
const page = ref(1)
const size = 12

// 抓取区间，改动要重新请求接口
const RANGES = [
  { value: '7:14', label: '近一周' },
  { value: '30:30', label: '近一月' },
  { value: '60:90', label: '全部' },
]
const range = ref('7:14')

// 番号、标题、演员任一命中即可
function matchKeyword(item, word) {
  return [item.code, item.cn_title, item.title, item.casts]
    .some((field) => (field || '').toLowerCase().includes(word))
}

// 发布状态之外的筛选，三个计数按钮共用，数字才和列表一致
const base = computed(() => {
  let list = items.value
  if (subFilter.value === 'yes') list = list.filter((i) => i.status >= 1)
  else if (subFilter.value === 'no') list = list.filter((i) => !i.status)

  const word = keyword.value.trim().toLowerCase()
  if (word) list = list.filter((i) => matchKeyword(i, word))
  return list
})

const upcoming = computed(() => base.value.filter((i) => i.upcoming))
const released = computed(() => base.value.filter((i) => !i.upcoming))

const shown = computed(() => {
  if (filter.value === 'upcoming') return upcoming.value
  if (filter.value === 'released') return released.value
  return base.value
})

const pages = computed(() => Math.max(Math.ceil(shown.value.length / size), 1))
const paged = computed(() => shown.value.slice((page.value - 1) * size, page.value * size))

// 筛选条件变化后当前页可能已越界
watch([filter, subFilter, keyword, active], () => {
  page.value = 1
})

async function load() {
  loading.value = true
  try {
    const data = await getBrands()
    brands.value = data.brands || []
    publishers.value = data.publishers || []
    if (!active.value && brands.value.length) {
      await select(brands.value[0].key)
    }
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

async function select(key) {
  active.value = key
  loadingList.value = true
  items.value = []
  error.value = ''
  try {
    const [past, future] = range.value.split(':').map(Number)
    const data = await getBrandCodes(key, past, future)
    items.value = data.items || []
  } catch (err) {
    // toast 会消失，抓取失败留在页面上更好排查
    error.value = err.message
    toast.error(err.message)
  } finally {
    loadingList.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <!-- 厂牌切换 -->
    <div class="flex flex-wrap gap-2">
      <button
        v-for="brand in brands"
        :key="brand.key"
        class="btn px-3 py-1.5 text-xs"
        :class="active === brand.key ? 'bg-brand text-white' : 'btn-ghost'"
        :disabled="loadingList"
        @click="select(brand.key)"
      >
        {{ brand.label }}
      </button>
    </div>

    <LoadingBlock v-if="loading && !brands.length" :rows="3" />

    <template v-else-if="active">
      <!-- 发布状态过滤 -->
      <div class="flex flex-wrap items-center gap-2">
        <button
          class="btn px-3 py-1 text-xs"
          :class="filter === 'all' ? 'bg-brand text-white' : 'btn-ghost'"
          @click="filter = 'all'"
        >
          全部 {{ base.length }}
        </button>
        <button
          class="btn px-3 py-1 text-xs"
          :class="filter === 'upcoming' ? 'bg-brand text-white' : 'btn-ghost'"
          @click="filter = 'upcoming'"
        >
          预定发布 {{ upcoming.length }}
        </button>
        <button
          class="btn px-3 py-1 text-xs"
          :class="filter === 'released' ? 'bg-brand text-white' : 'btn-ghost'"
          @click="filter = 'released'"
        >
          已发布 {{ released.length }}
        </button>
        <button class="btn-ghost px-3 py-1 text-xs" :disabled="loadingList" @click="select(active)">
          刷新
        </button>
      </div>

      <!-- 关键词、订阅状态、抓取区间 -->
      <div class="flex flex-wrap items-center gap-2">
        <input
          v-model="keyword"
          class="input w-full sm:w-56"
          placeholder="番号 / 标题 / 演员"
        />
        <select v-model="subFilter" class="input w-full sm:w-28">
          <option value="">全部状态</option>
          <option value="no">未订阅</option>
          <option value="yes">已订阅</option>
        </select>
        <select
          v-model="range"
          class="input w-full sm:w-28"
          :disabled="loadingList"
          @change="select(active)"
        >
          <option v-for="item in RANGES" :key="item.value" :value="item.value">
            {{ item.label }}
          </option>
        </select>
      </div>

      <LoadingBlock v-if="loadingList" :rows="4" />

      <div v-else-if="error" class="card space-y-1 border border-red-900/50">
        <p class="text-sm text-red-400">抓取失败</p>
        <p class="text-xs text-gray-400">{{ error }}</p>
        <p class="text-[11px] text-gray-600">
          厂牌官网在境外，服务器直连不通时需要在「其他」里配置代理
        </p>
      </div>

      <template v-else-if="shown.length">
        <div class="grid gap-3 lg:grid-cols-2 2xl:grid-cols-3">
          <div v-for="item in paged" :key="item.code" class="relative">
            <span
              v-if="item.upcoming"
              class="badge absolute right-2 top-2 z-10 bg-violet-900 text-violet-200"
            >
              预定 {{ item.release_date }}
            </span>
            <CodeCard :item="item" @changed="select(active)" />
          </div>
        </div>

        <div v-if="pages > 1" class="flex items-center justify-center gap-3 pt-2">
          <button class="btn-ghost px-3 py-1.5 text-xs" :disabled="page <= 1" @click="page--">
            上一页
          </button>
          <span class="text-xs tabular-nums text-gray-500">{{ page }} / {{ pages }}</span>
          <button class="btn-ghost px-3 py-1.5 text-xs" :disabled="page >= pages" @click="page++">
            下一页
          </button>
        </div>
      </template>

      <EmptyState
        v-else-if="items.length"
        text="没有符合条件的作品"
        hint="试试放宽关键词或订阅状态筛选"
      />

      <EmptyState
        v-else
        text="该厂牌暂无作品"
        hint="官网日期页可能没有数据，或抓取被拦截"
      />
    </template>

    <EmptyState v-else-if="!loading" text="暂无可抓取的厂牌" />

    <!-- 库中已有的发行商，仅展示 -->
    <div v-if="publishers.length" class="space-y-2 border-t border-gray-800 pt-4">
      <p class="text-xs text-gray-500">库中出现过的发行商</p>
      <div class="flex flex-wrap gap-2">
        <span
          v-for="name in publishers"
          :key="name"
          class="rounded-lg border border-gray-800 bg-gray-900 px-2.5 py-1 text-xs text-gray-400"
        >
          {{ name }}
        </span>
      </div>
    </div>
  </div>
</template>
