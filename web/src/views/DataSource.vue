<script setup>
import { computed, onMounted, ref } from 'vue'
import {
  checkAllDataSources, checkDataSource, listDataSources, updateDataSource,
} from '@/api'
import { useToast } from '@/composables/useToast'
import LoadingBlock from '@/components/LoadingBlock.vue'

const toast = useToast()
const items = ref([])
const loading = ref(false)
const checking = ref('')
const checkingAll = ref(false)
const editing = ref(null)
const draft = ref({})

const STATUS = {
  ok: { dot: 'bg-emerald-500', text: '正常' },
  blocked: { dot: 'bg-amber-500', text: '被拦截' },
  fail: { dot: 'bg-red-500', text: '不通' },
  '': { dot: 'bg-gray-600', text: '未测试' },
}

// 没有解析器的源只能测连通，不参与抓取，得让用户看清楚
const usable = computed(() => items.value.filter((i) => i.has_parser))
const registered = computed(() => items.value.filter((i) => !i.has_parser))

async function load() {
  loading.value = true
  try {
    const data = await listDataSources()
    items.value = data.items || []
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}

async function toggle(item) {
  const next = !item.enabled
  try {
    await updateDataSource(item.key, { enabled: next })
    item.enabled = next
  } catch (err) {
    toast.error(err.message)
  }
}

async function check(item) {
  checking.value = item.key
  try {
    const data = await checkDataSource(item.key)
    item.status = data.status
    item.status_message = data.message
    if (data.status === 'ok') toast.success(`${item.name} 连通正常`)
    else toast.error(`${item.name}：${data.message}`)
  } catch (err) {
    toast.error(err.message)
  } finally {
    checking.value = ''
  }
}

async function checkAll() {
  checkingAll.value = true
  try {
    const data = await checkAllDataSources()
    const byKey = Object.fromEntries((data.items || []).map((i) => [i.key, i]))
    items.value.forEach((item) => {
      const result = byKey[item.key]
      if (!result) return
      item.status = result.status
      item.status_message = result.message
    })
    const ok = (data.items || []).filter((i) => i.status === 'ok').length
    toast.success(`测试完成，${ok} / ${(data.items || []).length} 个正常`)
  } catch (err) {
    toast.error(err.message)
  } finally {
    checkingAll.value = false
  }
}

function open(item) {
  editing.value = item.key
  draft.value = {
    host: item.host,
    interval: item.interval,
    priority: item.priority,
    cookie: '',
    bypass_first: item.bypass_first,
  }
}

async function save() {
  const key = editing.value
  const payload = { ...draft.value }
  // 留空表示不改 cookie，避免把已配置的清掉
  if (!payload.cookie) delete payload.cookie
  payload.interval = Number(payload.interval) || 0
  payload.priority = Number(payload.priority) || 0

  try {
    await updateDataSource(key, payload)
    toast.success('已保存')
    editing.value = null
    await load()
  } catch (err) {
    toast.error(err.message)
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center gap-2">
      <p class="text-xs text-gray-500">点击卡片可改地址、请求间隔与 Cookie</p>
      <button
        class="btn-ghost ml-auto px-3 py-1.5 text-xs"
        :disabled="checkingAll"
        @click="checkAll"
      >
        {{ checkingAll ? '测试中…' : '测试全部' }}
      </button>
    </div>

    <LoadingBlock v-if="loading" :rows="4" />

    <template v-else>
      <!-- 已接入抓取的源 -->
      <div class="space-y-2">
        <p class="text-xs text-gray-500">已接入抓取</p>
        <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          <div
            v-for="item in usable"
            :key="item.key"
            class="card cursor-pointer space-y-2 transition-colors hover:border-gray-700"
            @click="open(item)"
          >
            <div class="flex items-center gap-2">
              <span class="h-2 w-2 shrink-0 rounded-full" :class="STATUS[item.status].dot" />
              <span class="truncate text-sm font-medium text-gray-200">{{ item.name }}</span>
              <button
                class="ml-auto shrink-0"
                :title="item.enabled ? '已启用' : '已停用'"
                @click.stop="toggle(item)"
              >
                <span
                  class="flex h-4 w-8 items-center rounded-full px-0.5 transition-colors"
                  :class="item.enabled ? 'bg-brand' : 'bg-gray-700'"
                >
                  <span
                    class="h-3 w-3 rounded-full bg-white transition-transform"
                    :class="item.enabled ? 'translate-x-4' : ''"
                  />
                </span>
              </button>
            </div>

            <p class="truncate text-[11px] text-gray-500">{{ item.host }}</p>

            <div class="flex flex-wrap items-center gap-1.5">
              <span v-if="item.interval" class="badge bg-gray-800 text-gray-400">
                CD {{ item.interval }}s
              </span>
              <span v-if="item.bypass_first" class="badge bg-amber-950 text-amber-300">
                需过盾
              </span>
              <span v-if="item.has_cookie" class="badge bg-gray-800 text-gray-400">
                已配 Cookie
              </span>
              <button
                class="btn-ghost ml-auto px-2 py-0.5 text-[11px]"
                :disabled="checking === item.key"
                @click.stop="check(item)"
              >
                {{ checking === item.key ? '测试中' : '测试' }}
              </button>
            </div>

            <p v-if="item.status_message" class="text-[11px] text-gray-600">
              {{ item.status_message }}
            </p>
          </div>
        </div>
      </div>

      <!-- 仅登记，尚无解析器 -->
      <div v-if="registered.length" class="space-y-2 border-t border-gray-800 pt-4">
        <p class="text-xs text-gray-500">
          尚未接入解析（可测连通性，但不参与抓取）
        </p>
        <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          <div
            v-for="item in registered"
            :key="item.key"
            class="card cursor-pointer space-y-2 opacity-70 transition-colors hover:border-gray-700 hover:opacity-100"
            @click="open(item)"
          >
            <div class="flex items-center gap-2">
              <span class="h-2 w-2 shrink-0 rounded-full" :class="STATUS[item.status].dot" />
              <span class="truncate text-sm text-gray-300">{{ item.name }}</span>
              <button
                class="btn-ghost ml-auto shrink-0 px-2 py-0.5 text-[11px]"
                :disabled="checking === item.key"
                @click.stop="check(item)"
              >
                {{ checking === item.key ? '测试中' : '测试' }}
              </button>
            </div>
            <p class="truncate text-[11px] text-gray-500">{{ item.host }}</p>
            <div v-if="item.bypass_first">
              <span class="badge bg-amber-950 text-amber-300">需过盾</span>
            </div>
            <p v-if="item.status_message" class="text-[11px] text-gray-600">
              {{ item.status_message }}
            </p>
          </div>
        </div>
      </div>
    </template>

    <!-- 详细设置 -->
    <div
      v-if="editing"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      @click.self="editing = null"
    >
      <div class="card w-full max-w-md space-y-3">
        <p class="text-sm font-medium text-gray-200">{{ editing }} 设置</p>

        <div>
          <label class="label">地址</label>
          <input v-model="draft.host" class="input" placeholder="https://example.com" />
        </div>

        <div class="grid gap-3 sm:grid-cols-2">
          <div>
            <label class="label">请求间隔（秒）</label>
            <input v-model="draft.interval" class="input" placeholder="0 表示用默认" />
          </div>
          <div>
            <label class="label">优先级</label>
            <input v-model="draft.priority" class="input" placeholder="越小越优先" />
          </div>
        </div>

        <div>
          <label class="label">Cookie</label>
          <textarea
            v-model="draft.cookie"
            class="input font-mono text-xs"
            rows="2"
            placeholder="留空表示不修改"
          />
        </div>

        <label class="flex items-center gap-2">
          <input v-model="draft.bypass_first" type="checkbox" class="h-4 w-4 accent-emerald-500" />
          <span class="text-sm text-gray-400">直接走反爬绕过服务</span>
        </label>

        <div class="flex justify-end gap-2 pt-1">
          <button class="btn-ghost px-3 py-1.5 text-xs" @click="editing = null">取消</button>
          <button class="btn-primary px-3 py-1.5 text-xs" @click="save">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>
