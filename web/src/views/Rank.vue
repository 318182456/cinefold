<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getRank, subscribeRank, subscribeCode } from '@/api'
import { useToast } from '@/composables/useToast'
import { useCodeSelection } from '@/composables/useCodeSelection'
import CodeCard from '@/components/CodeCard.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import SelectionBar from '@/components/SelectionBar.vue'

const toast = useToast()
const items = ref([])
const loading = ref(false)
const rankType = ref('daily')
const page = ref(1)
const size = 12

const TYPES = [
  { value: 'daily', label: '日榜' },
  { value: 'weekly', label: '周榜' },
  { value: 'monthly', label: '月榜' },
]

// 榜单是一次抓取整批后缓存的，接口没有 offset/total，所以在前端切页
const pages = computed(() => Math.max(Math.ceil(items.value.length / size), 1))
const paged = computed(() => items.value.slice((page.value - 1) * size, page.value * size))

async function load() {
  loading.value = true
  try {
    const data = await getRank(rankType.value)
    items.value = data.items || []
    // 订阅后重新加载，条数变少时当前页可能已越界
    if (page.value > pages.value) page.value = pages.value
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

const sel = useCodeSelection(paged, load)

watch(rankType, () => {
  page.value = 1
  sel.clear()
  load()
})
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

    <SelectionBar v-if="items.length" :sel="sel" />

    <LoadingBlock v-if="loading" :rows="4" />

    <template v-else-if="items.length">
      <div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <template v-for="item in paged" :key="item.code">
          <CodeCard
            v-if="item.title || item.status !== undefined"
            :item="item"
            :selectable="sel.active.value"
            :selected="sel.isSelected(item.code)"
            @toggle-select="sel.toggle"
            @changed="load"
          />
          <div
            v-else
            class="card flex items-center justify-between"
            :class="[
              sel.active.value ? 'cursor-pointer' : '',
              sel.active.value && sel.isSelected(item.code) ? 'border-brand bg-brand/5' : '',
            ]"
            @click="sel.active.value && sel.toggle(item.code)"
          >
            <span class="flex items-center gap-2">
              <input
                v-if="sel.active.value"
                type="checkbox"
                class="h-3.5 w-3.5 cursor-pointer accent-brand"
                :checked="sel.isSelected(item.code)"
                @click.stop="sel.toggle(item.code)"
              />
              <span class="font-mono text-sm text-brand">{{ item.code }}</span>
            </span>
            <button class="btn-primary px-2.5 py-1 text-xs" @click.stop="subOne(item.code)">
              订阅
            </button>
          </div>
        </template>
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
      v-else
      text="榜单为空"
      hint="资源站不可达时会退回本地高分番号，请检查网络与代理配置"
    />
  </div>
</template>
