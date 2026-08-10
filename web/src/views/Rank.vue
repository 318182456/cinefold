<script setup>
import { onMounted, ref, watch } from 'vue'
import { getRank, subscribeRank, subscribeCode } from '@/api'
import { useToast } from '@/composables/useToast'
import CodeCard from '@/components/CodeCard.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'

const toast = useToast()
const items = ref([])
const loading = ref(false)
const rankType = ref('daily')

const TYPES = [
  { value: 'daily', label: '日榜' },
  { value: 'weekly', label: '周榜' },
  { value: 'monthly', label: '月榜' },
]

async function load() {
  loading.value = true
  try {
    const data = await getRank(rankType.value)
    items.value = data.items || []
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

async function subscribeAll() {
  try {
    await subscribeRank()
    toast.success('榜单订阅任务已触发')
  } catch (err) {
    toast.error(err.message)
  }
}

async function subOne(code) {
  try {
    await subscribeCode(code)
    toast.success(`已订阅 ${code}`)
    load()
  } catch (err) {
    toast.error(err.message)
  }
}

watch(rankType, load)
onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center gap-2">
      <button
        v-for="type in TYPES"
        :key="type.value"
        class="btn px-3 py-1.5 text-xs"
        :class="rankType === type.value ? 'bg-brand text-white' : 'btn-ghost'"
        @click="rankType = type.value"
      >
        {{ type.label }}
      </button>
      <button class="btn-ghost ml-auto px-3 py-1.5 text-xs" @click="subscribeAll">
        一键订阅榜单
      </button>
    </div>

    <LoadingBlock v-if="loading" :rows="4" />

    <div v-else-if="items.length" class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      <template v-for="item in items" :key="item.code">
        <CodeCard v-if="item.title || item.status !== undefined" :item="item" @changed="load" />
        <div v-else class="card flex items-center justify-between">
          <span class="font-mono text-sm text-brand">{{ item.code }}</span>
          <button class="btn-primary px-2.5 py-1 text-xs" @click="subOne(item.code)">订阅</button>
        </div>
      </template>
    </div>

    <EmptyState
      v-else
      text="榜单为空"
      hint="资源站不可达时会退回本地高分番号，请检查网络与代理配置"
    />
  </div>
</template>
