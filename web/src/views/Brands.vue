<script setup>
import { onMounted, ref } from 'vue'
import { getBrands } from '@/api'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/EmptyState.vue'

const toast = useToast()
const items = ref([])
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const data = await getBrands()
    items.value = data.items || []
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <p class="text-xs text-gray-500">
      库中出现过的发行商。厂牌自动订阅需在设置页配置 BRAND_TYPE
    </p>

    <div v-if="items.length" class="flex flex-wrap gap-2">
      <span
        v-for="brand in items"
        :key="brand"
        class="rounded-lg border border-gray-800 bg-gray-900 px-3 py-1.5 text-sm text-gray-300"
      >
        {{ brand }}
      </span>
    </div>

    <EmptyState v-else-if="!loading" text="暂无厂牌数据" hint="番号详情补全后会自动归类" />
  </div>
</template>
