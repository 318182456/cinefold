<script setup>
import { computed, ref } from 'vue'
import {
  codeCover,
  subscribeCode,
  cancelCode,
  downloadCode,
  refetchCover,
  resetCode,
  translateCodeTitle,
} from '@/api'
import { useToast } from '@/composables/useToast'
import { useConfigStore } from '@/stores/config'
import { parseCasts } from '@/utils/cast'
import ImageLightbox from './ImageLightbox.vue'

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
// 灯箱看的是完整原图，卡片上那半边只是可视窗口挪过去了
const lightboxOpen = ref(false)

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

// 源站封面常是横版双拼图：一半碟片封套，一半人像正片。图完整存着，
// 这里把可视窗口挪到人像那半边；判断不出来（portrait_side 为空或 none）
// 就居中显示，跟普通封面一样
const coverPosition = computed(() => {
  const side = props.item.portrait_side
  if (side === 'left') return 'left center'
  if (side === 'right') return 'right center'
  return 'center'
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

// 源站换图、或人像面判错时用它刷新
async function refetch() {
  refetching.value = true
  try {
    const data = await refetchCover(props.item.code)
    if (data?.local_banner) {
      props.item.local_banner = data.local_banner
    }
    // 重新判断的结果要立刻反映到卡片上，否则还按旧的那半边显示
    props.item.portrait_side = data?.portrait_side ?? ''
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

const translating = ref(false)

// 定时任务只翻没有译文的番号，所以机翻一旦把标题译坏就再也不会自己重来。
// 这个按钮强制重译一遍，译好的标题直接写回卡片，不用刷新整页
async function translateTitle() {
  translating.value = true
  try {
    // http 拦截器已经把统一响应体拆掉了，返回的就是 data 本身，别再取一层
    const data = (await translateCodeTitle(props.item.code)) || {}
    if (data.cn_title) {
      props.item.cn_title = data.cn_title
    }
    toast.success(
      data.changed === false ? `${props.item.code} 的译文与原来一致` : `已翻译 ${props.item.code}`,
    )
  } catch (err) {
    toast.error(err.message)
  } finally {
    translating.value = false
  }
}

const resetting = ref(false)

// 文件删干净了却一直报「已存在」时用这个。先预览再确认 —— 它会删下载记录
// 并重置状态，看清了再动手
async function reset() {
  resetting.value = true
  try {
    const preview = (await resetCode(props.item.code, true)) || {}
    const lines = []
    if (preview.cache_cleared) lines.push('· 清掉媒体库判定缓存')
    const removed = (preview.history_removed || []).length
    const kept = (preview.history_kept || []).length
    if (removed) lines.push(`· 删掉 ${removed} 条下载记录（下载器里已无对应种子）`)
    if (kept) lines.push(`· 保留 ${kept} 条（种子仍在做种，不动）`)
    if (preview.status_reset) lines.push('· 重置订阅状态，使其可重新搜种')
    if (preview.downloader_unavailable) {
      lines.push('⚠ 下载器当前连不上，无法判断种子死活，下载记录一条都不会删')
    }
    if (!lines.length) lines.push('· 没有发现需要清理的残留')

    const ok = window.confirm(
      [`重置 ${props.item.code}，将执行：`, '', ...lines, '', '继续？'].join('\n'),
    )
    if (!ok) return

    const res = await resetCode(props.item.code, false)
    toast.success(res.message || `${props.item.code} 已可重新下载`)
    emit('changed')
  } catch (err) {
    toast.error(err.message)
  } finally {
    resetting.value = false
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
      @click.stop="selectable ? emit('toggle-select', item.code) : (lightboxOpen = true)"
    >
      <img
        v-if="cover && !imageFailed"
        :src="cover"
        :alt="item.code"
        loading="lazy"
        title="点击查看完整封面"
        class="h-full w-full cursor-pointer object-cover"
        :class="imageClass"
        :style="{ objectPosition: coverPosition }"
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
          title="重新下载封面并重新判断人像面"
          @click.stop="refetch"
        >
          {{ refetching ? '重抓中…' : '重抓' }}
        </button>
        <button
          class="btn-ghost px-2.5 py-1 text-xs"
          :disabled="translating"
          title="重新翻译标题，覆盖现有译文"
          @click.stop="translateTitle"
        >
          {{ translating ? '翻译中…' : '翻译' }}
        </button>
        <button
          class="btn-ghost px-2.5 py-1 text-xs"
          :disabled="resetting"
          title="文件已删却一直报「已存在」下不下来时，清掉拦住它的残留记录与缓存"
          @click.stop="reset"
        >
          {{ resetting ? '重置中…' : '重置' }}
        </button>
      </div>
    </div>

    <ImageLightbox
      :src="lightboxOpen ? cover : ''"
      :alt="item.code"
      @close="lightboxOpen = false"
    />
  </div>
</template>
