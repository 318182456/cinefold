<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getRecommend } from '@/api'
import { useToast } from '@/composables/useToast'
import { useCodeSelection } from '@/composables/useCodeSelection'
import CodeCard from '@/components/CodeCard.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import SelectionBar from '@/components/SelectionBar.vue'

const toast = useToast()
const items = ref([])
const total = ref(0)
const page = ref(1)
const size = 12
const loading = ref(false)

const pages = computed(() => Math.max(Math.ceil(total.value / size), 1))

async function load() {
  loading.value = true
  try {
    const data = await getRecommend(size, page.value)
    items.value = data.items || []
    total.value = data.total || 0
    // 订阅后这些番号就从推荐里消失了，末页可能已越界
    if (page.value > pages.value) {
      page.value = pages.value
      return
    }
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

const sel = useCodeSelection(items, load)

watch(page, () => {
  sel.clear()
  load()
})
onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <p class="text-xs text-gray-500">按评分排序的未订阅番号</p>

    <SelectionBar v-if="items.length" :sel="sel" />

    <LoadingBlock v-if="loading" :rows="4" />

    <template v-else-if="items.length">
      <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
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

    <EmptyState v-else text="暂无推荐" hint="等待抓取任务补全番号评分后会出现" />
  </div>
</template>
