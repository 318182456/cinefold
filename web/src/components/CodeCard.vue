<script setup>
import { computed, ref } from 'vue'
import { codeCover, subscribeCode, cancelCode, downloadCode } from '@/api'
import { useToast } from '@/composables/useToast'
import { useConfigStore } from '@/stores/config'

const props = defineProps({
  item: { type: Object, required: true },
})
const emit = defineEmits(['changed', 'detail'])

const toast = useToast()
const configStore = useConfigStore()
const busy = ref(false)
const imageFailed = ref(false)

const STATUS = {
  0: { text: '未订阅', class: 'bg-gray-700 text-gray-300' },
  1: { text: '已订阅', class: 'bg-blue-900 text-blue-300' },
  2: { text: '下载中', class: 'bg-amber-900 text-amber-300' },
  3: { text: '已下载', class: 'bg-emerald-900 text-emerald-300' },
  4: { text: '已入库', class: 'bg-emerald-800 text-emerald-200' },
  5: { text: '失败', class: 'bg-red-900 text-red-300' },
}

const status = computed(() => STATUS[props.item.status] ?? STATUS[0])
const subscribed = computed(() => props.item.status >= 1)
const title = computed(() => props.item.cn_title || props.item.title || '')
const cover = computed(() => codeCover(props.item))

// 图片模式跟随后端配置
const imageClass = computed(() => {
  const mode = configStore.imageMode
  if (mode === 'BLUR') return 'img-blur'
  if (mode === 'INVISIBLE') return 'img-hidden'
  return ''
})

const casts = computed(() =>
  (props.item.casts || '').split(',').filter(Boolean).slice(0, 3),
)

async function toggleSubscribe() {
  busy.value = true
  try {
    if (subscribed.value) {
      await cancelCode(props.item.code)
      toast.success(`已取消订阅 ${props.item.code}`)
    } else {
      await subscribeCode(props.item.code)
      toast.success(`已订阅 ${props.item.code}`)
    }
    emit('changed')
  } catch (err) {
    toast.error(err.message)
  } finally {
    busy.value = false
  }
}

async function download() {
  busy.value = true
  try {
    await downloadCode({ code: props.item.code })
    toast.success(`已推送 ${props.item.code} 到下载器`)
    emit('changed')
  } catch (err) {
    toast.error(err.message)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="card flex gap-3 transition-colors hover:border-gray-700">
    <!-- 封面 -->
    <div
      class="relative h-28 w-20 shrink-0 overflow-hidden rounded-lg bg-gray-800"
      @click="emit('detail', item)"
    >
      <img
        v-if="cover && !imageFailed"
        :src="cover"
        :alt="item.code"
        loading="lazy"
        class="h-full w-full cursor-pointer object-cover"
        :class="imageClass"
        @error="imageFailed = true"
      />
      <div v-else class="flex h-full w-full items-center justify-center text-[10px] text-gray-600">
        无封面
      </div>
    </div>

    <!-- 信息 -->
    <div class="flex min-w-0 flex-1 flex-col">
      <div class="flex items-start justify-between gap-2">
        <span class="font-mono text-sm font-semibold text-brand">{{ item.code }}</span>
        <span class="badge shrink-0" :class="status.class">{{ status.text }}</span>
      </div>

      <p class="mt-1 line-clamp-2 text-sm text-gray-300" :title="title">
        {{ title || '—' }}
      </p>

      <div class="mt-auto flex flex-wrap items-center gap-x-3 gap-y-1 pt-2 text-[11px] text-gray-500">
        <span v-if="item.release_date">{{ item.release_date }}</span>
        <span v-if="item.star">★ {{ item.star }}</span>
        <span v-for="cast in casts" :key="cast" class="truncate">{{ cast }}</span>
      </div>

      <div class="mt-2 flex gap-2">
        <button
          class="btn px-2.5 py-1 text-xs"
          :class="subscribed ? 'btn-ghost' : 'btn-primary'"
          :disabled="busy"
          @click="toggleSubscribe"
        >
          {{ subscribed ? '取消订阅' : '订阅' }}
        </button>
        <button class="btn-ghost px-2.5 py-1 text-xs" :disabled="busy" @click="download">
          下载
        </button>
      </div>
    </div>
  </div>
</template>
