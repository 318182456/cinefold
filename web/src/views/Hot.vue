<script setup>
import { onMounted, ref } from 'vue'
import { getRecommend } from '@/api'
import { useToast } from '@/composables/useToast'
import { useCodeSelection } from '@/composables/useCodeSelection'
import CodeCard from '@/components/CodeCard.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import SelectionBar from '@/components/SelectionBar.vue'

const toast = useToast()
const items = ref([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const data = await getRecommend(30)
    items.value = data.items || []
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

const sel = useCodeSelection(items, load)

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <p class="text-xs text-gray-500">按评分排序的未订阅番号</p>

    <SelectionBar v-if="items.length" :sel="sel" />

    <LoadingBlock v-if="loading" :rows="4" />
    <div v-else-if="items.length" class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
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
    <EmptyState v-else text="暂无推荐" hint="等待抓取任务补全番号评分后会出现" />
  </div>
</template>
