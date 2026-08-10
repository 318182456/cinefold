<script setup>
import { computed, onMounted, ref } from 'vue'
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

const upcoming = computed(() => items.value.filter((i) => i.upcoming))
const released = computed(() => items.value.filter((i) => !i.upcoming))

const shown = computed(() => {
  if (filter.value === 'upcoming') return upcoming.value
  if (filter.value === 'released') return released.value
  return items.value
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
    const data = await getBrandCodes(key)
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
          全部 {{ items.length }}
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

      <LoadingBlock v-if="loadingList" :rows="4" />

      <div v-else-if="error" class="card space-y-1 border border-red-900/50">
        <p class="text-sm text-red-400">抓取失败</p>
        <p class="text-xs text-gray-400">{{ error }}</p>
        <p class="text-[11px] text-gray-600">
          厂牌官网在境外，服务器直连不通时需要在「其他」里配置代理
        </p>
      </div>

      <div v-else-if="shown.length" class="grid gap-3 lg:grid-cols-2 2xl:grid-cols-3">
        <div v-for="item in shown" :key="item.code" class="relative">
          <span
            v-if="item.upcoming"
            class="badge absolute right-2 top-2 z-10 bg-violet-900 text-violet-200"
          >
            预定 {{ item.release_date }}
          </span>
          <CodeCard :item="item" @changed="select(active)" />
        </div>
      </div>

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
