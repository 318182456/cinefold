<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { listCodes } from '@/api'
import { useToast } from '@/composables/useToast'
import CodeCard from '@/components/CodeCard.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'

const toast = useToast()
const items = ref([])
const total = ref(0)
const page = ref(1)
const size = 30
const status = ref(1)
const loading = ref(false)

const TABS = [
  { value: 1, label: '已订阅' },
  { value: 2, label: '下载中' },
  { value: 3, label: '已下载' },
  { value: 4, label: '已入库' },
  { value: 5, label: '失败' },
  { value: null, label: '全部' },
]

const pages = computed(() => Math.max(Math.ceil(total.value / size), 1))

async function load() {
  loading.value = true
  try {
    const params = { page: page.value, size }
    if (status.value !== null) params.status = status.value
    const data = await listCodes(params)
    items.value = data.items || []
    total.value = data.total || 0
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

watch(status, () => {
  page.value = 1
  load()
})
watch(page, load)
onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap gap-2">
      <button
        v-for="tab in TABS"
        :key="String(tab.value)"
        class="btn px-3 py-1.5 text-xs"
        :class="status === tab.value ? 'bg-brand text-white' : 'btn-ghost'"
        @click="status = tab.value"
      >
        {{ tab.label }}
      </button>
    </div>

    <LoadingBlock v-if="loading" :rows="4" />

    <template v-else-if="items.length">
      <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <CodeCard v-for="item in items" :key="item.code" :item="item" @changed="load" />
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

    <EmptyState v-else text="这里还没有内容" hint="可以在搜索页添加订阅" />
  </div>
</template>
