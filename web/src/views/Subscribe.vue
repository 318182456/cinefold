<script setup>
import PagePanel from '@/components/PagePanel.vue'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listCodes } from '@/api'
import { useToast } from '@/composables/useToast'
import { useCodeSelection } from '@/composables/useCodeSelection'
import CodeCard from '@/components/CodeCard.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import SelectionBar from '@/components/SelectionBar.vue'

const toast = useToast()
const route = useRoute(), router = useRouter()
const items = ref([])
const total = ref(0)
const page = ref(1)
const size = 12
const status = ref(1)
const loading = ref(false)

const TABS = [
  { value: 0, label: '未订阅' },
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

const sel = useCodeSelection(items, load)

function chooseStatus(value) {
  router.push({ query: { ...route.query, status: value === null ? 'all' : String(value) } })
}
watch(() => route.query.status, value => {
  status.value = value === 'all' ? null : /^[0-5]$/.test(String(value ?? '')) ? Number(value) : 1
  sel.clear()
  if (page.value !== 1) page.value = 1
  else load()
}, { immediate: true })
watch(page, load)
</script>

<template>
  <PagePanel content-class="space-y-4">
    <template #header>

    <div class="flex flex-wrap gap-2">
      <button
        v-for="tab in TABS"
        :key="String(tab.value)"
        class="btn px-3 py-1.5 text-xs"
        :class="status === tab.value ? 'bg-brand text-white' : 'btn-ghost'"
        @click="chooseStatus(tab.value)"
      >
        {{ tab.label }}
      </button>
    </div>

    <SelectionBar v-if="items.length" :sel="sel" action="cancel" />
    </template>


    <LoadingBlock v-if="loading" :rows="4" />

    <template v-else-if="items.length">
      <div class="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
        <CodeCard
          v-for="item in items"
          :key="item.code"
          :item="item"
          :selectable="sel.active.value"
          :selected="sel.isSelected(item.code)"
          @toggle-select="sel.toggle"
          @changed="load"
        />
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
  </PagePanel>
</template>
