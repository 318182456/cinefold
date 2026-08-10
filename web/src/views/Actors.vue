<script setup>
import { onMounted, ref } from 'vue'
import { listActors, subscribeActor, cancelActor, getActorCodes, proxyImage } from '@/api'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import CodeCard from '@/components/CodeCard.vue'

const toast = useToast()
const items = ref([])
const loading = ref(false)
const keyword = ref('')

const newName = ref('')
const newDate = ref('')

const selected = ref(null)
const codes = ref([])
const loadingCodes = ref(false)

async function load() {
  loading.value = true
  try {
    const data = await listActors({ keyword: keyword.value.trim(), size: 60 })
    items.value = data.items || []
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

async function add() {
  const name = newName.value.trim()
  if (!name) return
  try {
    await subscribeActor(name, newDate.value)
    toast.success(`已订阅 ${name}`)
    newName.value = ''
    newDate.value = ''
    load()
  } catch (err) {
    toast.error(err.message)
  }
}

async function remove(name) {
  try {
    await cancelActor(name)
    toast.success(`已取消订阅 ${name}`)
    if (selected.value === name) selected.value = null
    load()
  } catch (err) {
    toast.error(err.message)
  }
}

async function showCodes(name) {
  selected.value = name
  loadingCodes.value = true
  codes.value = []
  try {
    const data = await getActorCodes(name)
    codes.value = data.items || []
  } catch (err) {
    toast.error(err.message)
  } finally {
    loadingCodes.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-5">
    <!-- 新增订阅 -->
    <div class="card space-y-3">
      <p class="text-sm font-medium text-gray-300">订阅演员</p>
      <div class="flex flex-col gap-2 sm:flex-row">
        <input v-model="newName" class="input sm:flex-1" placeholder="演员名" @keyup.enter="add" />
        <input
          v-model="newDate"
          class="input sm:w-44"
          placeholder="起始日期 2024-01-01"
        />
        <button class="btn-primary sm:w-24" @click="add">添加</button>
      </div>
      <p class="text-xs text-gray-600">
        填写起始日期后，只订阅该日期之后发行的作品
      </p>
    </div>

    <!-- 搜索 -->
    <div class="flex gap-2">
      <input v-model="keyword" class="input flex-1" placeholder="筛选已订阅演员" @keyup.enter="load" />
      <button class="btn-ghost" @click="load">筛选</button>
    </div>

    <LoadingBlock v-if="loading" :rows="3" />

    <div v-else-if="items.length" class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
      <div
        v-for="actor in items"
        :key="actor.name"
        class="card flex items-center gap-3 transition-colors hover:border-gray-700"
      >
        <img
          v-if="actor.photo"
          :src="proxyImage(actor.photo)"
          :alt="actor.name"
          loading="lazy"
          class="h-14 w-14 shrink-0 rounded-full object-cover"
        />
        <div v-else class="h-14 w-14 shrink-0 rounded-full bg-gray-800" />

        <div class="min-w-0 flex-1">
          <p class="truncate text-sm font-medium text-gray-200">{{ actor.name }}</p>
          <p v-if="actor.limit_date" class="text-[11px] text-gray-500">
            {{ actor.limit_date }} 起
          </p>
          <div class="mt-1.5 flex gap-2">
            <button class="text-[11px] text-brand hover:underline" @click="showCodes(actor.name)">
              作品
            </button>
            <button class="text-[11px] text-red-400 hover:underline" @click="remove(actor.name)">
              取消
            </button>
          </div>
        </div>
      </div>
    </div>

    <EmptyState v-else text="还没有订阅演员" hint="订阅后会自动追踪其新作品" />

    <!-- 作品列表 -->
    <section v-if="selected" class="space-y-3">
      <h2 class="text-sm font-medium text-gray-300">{{ selected }} 的作品</h2>
      <LoadingBlock v-if="loadingCodes" :rows="2" />
      <div v-else-if="codes.length" class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <CodeCard v-for="item in codes" :key="item.code" :item="item" @changed="showCodes(selected)" />
      </div>
      <EmptyState v-else text="库中还没有该演员的作品" />
    </section>
  </div>
</template>
