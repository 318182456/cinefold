<script setup>
import { computed, ref } from 'vue'
import { codeCover, subscribeCode, cancelCode, downloadCode, refetchCover } from '@/api'
import { useToast } from '@/composables/useToast'
import { useConfigStore } from '@/stores/config'
import { parseCasts } from '@/utils/cast'

const props = defineProps({
  item: { type: Object, required: true },
  // 多选模式：显示勾选框，卡片空白处点击即切换选中
  selectable: { type: Boolean, default: false },
  selected: { type: Boolean, default: false },
})
const emit = defineEmits(['changed', 'detail', 'toggle-select'])

const toast = useToast()
const configStore = useConfigStore()
const busy = ref(false)
const refetching = ref(false)
const imageFailed = ref(false)
// 重抓后 URL 不变，浏览器会拿着 30 天缓存不动，靠它逼一次重新请求
const coverVersion = ref(0)

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
const cover = computed(() => {
  const url = codeCover(props.item)
  if (!url) return ''
  if (!coverVersion.value) return url
  return `${url}${url.includes('?') ? '&' : '?'}_v=${coverVersion.value}`
})

// 图片模式跟随后端配置
const imageClass = computed(() => {
  const mode = configStore.imageMode
  if (mode === 'BLUR') return 'img-blur'
  if (mode === 'INVISIBLE') return 'img-hidden'
  return ''
})

// 数据源会把年龄、职业设定、日文原名一起塞进 casts，清洗后再显示
const casts = computed(() => parseCasts(props.item.casts, 3))

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

// 裁剪是覆盖原图的，裁错边只能靠重抓补救
async function refetch() {
  refetching.value = true
  try {
    const data = await refetchCover(props.item.code)
    if (data?.local_banner) {
      props.item.local_banner = data.local_banner
    }
    imageFailed.value = false
    coverVersion.value = Date.now()
    toast.success(`已重抓 ${props.item.code} 的封面`)
  } catch (err) {
    toast.error(err.message)
  } finally {
    refetching.value = false
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
  <div
    class="card flex gap-3 transition-colors hover:border-gray-700"
    :class="[
      selectable ? 'cursor-pointer' : '',
      selectable && selected ? 'border-brand bg-brand/5' : '',
    ]"
    @click="selectable && emit('toggle-select', item.code)"
  >
    <!-- 封面 -->
    <div
      class="relative h-28 w-20 shrink-0 overflow-hidden rounded-lg bg-gray-800"
      @click.stop="selectable ? emit('toggle-select', item.code) : emit('detail', item)"
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
        <input
          v-if="selectable"
          type="checkbox"
          class="mt-0.5 h-3.5 w-3.5 shrink-0 cursor-pointer accent-brand"
          :checked="selected"
          @click.stop="emit('toggle-select', item.code)"
        />
        <span class="mr-auto font-mono text-sm font-semibold text-brand">{{ item.code }}</span>
        <span class="badge shrink-0" :class="status.class">{{ status.text }}</span>
      </div>

      <p class="mt-1 line-clamp-2 text-sm text-gray-300" :title="title">
        {{ title || '—' }}
      </p>

      <div class="mt-auto flex flex-wrap items-center gap-x-3 gap-y-1 pt-2 text-[11px] text-gray-500">
        <span v-if="item.release_date" :class="item.upcoming ? 'text-violet-300' : ''">
          <span v-if="item.upcoming" class="mr-1">预定</span>{{ item.release_date }}
        </span>
        <span v-if="item.star">★ {{ item.star }}</span>
        <!-- 演员名可以很长（数据源里有「エミリサン 26岁 株トレーダー
             (エミリサン 26歳 株トレーダー)」这种），而 truncate 自带 nowrap。
             不给 min-w-0，flex item 的 min-width:auto 不许它收缩到内容宽度以下，
             它就拿满整行、把卡片顶宽，truncate 反而永远不触发。
             放这张卡片的 grid 还必须写 grid-cols-1（而非省略），否则列的
             minmax 下限是 auto，同样会被这行字撑开、整页横向溢出 -->
        <!-- 名字已清洗过，但仍保留 min-w-0：外国艺名之类可能本来就很长，
             而 truncate 自带 nowrap，flex item 的 min-width:auto 会让它
             拿满整行、把卡片连同整页撑破，truncate 反而永远不触发。
             放这张卡片的 grid 还必须写 grid-cols-1，理由同上 —— 列的
             minmax 下限是 auto 时同样会被撑开 -->
        <span
          v-for="cast in casts"
          :key="cast"
          class="min-w-0 max-w-full truncate"
          :title="item.casts"
          >{{ cast }}</span
        >
      </div>

      <div class="mt-2 flex gap-2">
        <button
          class="btn px-2.5 py-1 text-xs"
          :class="subscribed ? 'btn-ghost' : 'btn-primary'"
          :disabled="busy"
          @click.stop="toggleSubscribe"
        >
          {{ subscribed ? '取消订阅' : '订阅' }}
        </button>
        <button class="btn-ghost px-2.5 py-1 text-xs" :disabled="busy" @click.stop="download">
          下载
        </button>
        <button
          class="btn-ghost px-2.5 py-1 text-xs"
          :disabled="refetching"
          title="重新下载封面并重新裁剪"
          @click.stop="refetch"
        >
          {{ refetching ? '重抓中…' : '重抓' }}
        </button>
      </div>
    </div>
  </div>
</template>
