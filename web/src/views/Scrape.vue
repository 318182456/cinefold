<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { previewScrape, runScrape, getScrapeFields, getConfig } from '@/api'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/EmptyState.vue'

const toast = useToast()

const form = reactive({
  path: '',
  // 留空用设置里的「刮削输出目录」
  target_dir: '',
  // 人工指定番号，认不出来时用
  code: '',
  // 库里已有元数据时是否仍去抓
  fetch_meta: true,
})

const previewing = ref(false)
const running = ref(false)
// 试算结果。null 表示还没算过，与「算过但没有影片」要分开显示
const result = ref(null)
const runResult = ref(null)
const fields = ref([])
const jinjaAvailable = ref(true)
const mode = ref('')
const showFields = ref(false)

// 模板语法示例。写成常量而不是直接放进模板 —— 花括号会被 Vue 当插值解析
const SYNTAX_BASIC = '{number}'
const SYNTAX_JINJA = '{{ number | upper }}'

const items = computed(() => result.value?.items || [])
const unknown = computed(() => result.value?.unknown || [])

// 试算过且有产物才允许开刮 —— 强制先看一眼产物路径再动手。
// 刮削会真的建硬链接写文件，路径错了要手工收拾
const canRun = computed(() => items.value.length > 0 && !running.value)

const trailerCount = computed(() => items.value.filter((i) => i.trailer).length)
const noMetaCount = computed(() => items.value.filter((i) => !i.has_meta).length)

onMounted(async () => {
  try {
    const [data, config] = await Promise.all([getScrapeFields(), getConfig()])
    fields.value = data.fields || []
    jinjaAvailable.value = data.jinja_available !== false
    mode.value = config?.scrape_mode || ''
  } catch {
    // 字段清单只是模板提示，取不到不影响试算
  }
})

async function doPreview() {
  if (!form.path.trim()) {
    toast.error('请填写要刮削的文件或目录路径')
    return
  }
  previewing.value = true
  runResult.value = null
  try {
    result.value = await previewScrape({ ...form, path: form.path.trim() })
  } catch (err) {
    result.value = null
    toast.error(err?.message || '试算失败')
  } finally {
    previewing.value = false
  }
}

async function doRun() {
  running.value = true
  try {
    runResult.value = await runScrape({ ...form, path: form.path.trim() })
    const { ok = 0, total = 0 } = runResult.value || {}
    if (ok === total) toast.success(`刮削完成，${ok} 个番号`)
    else toast.error(`${total} 个番号中 ${total - ok} 个未完成，详见下方`)
    // 产物已经变了，旧的试算结果不再成立
    result.value = null
  } catch (err) {
    toast.error(err?.message || '刮削失败')
  } finally {
    running.value = false
  }
}
</script>

<template>
  <div class="space-y-4">
    <!-- 当前刮削方式。走 external 时这个页面只能手动刮，说清楚免得白等 -->
    <div v-if="mode && mode !== 'builtin'" class="card">
      <p class="text-[11px] text-amber-400">
        当前刮削方式是「external」，下载完成后不会自动刮削 —— 那由 MDCng 等外部工具负责。
        这个页面仍可手动刮任意路径。要改成自动，去
        <RouterLink :to="{ name: 'Config' }" class="text-brand hover:underline">设置 → 刮削</RouterLink>
        把「由谁刮削」切成 builtin。
      </p>
    </div>

    <!-- 输入 -->
    <div class="card space-y-3">
      <div>
        <p class="text-sm font-medium text-gray-300">刮削试算</p>
        <p class="mt-0.5 text-[11px] text-gray-600">
          先试算看产物落在哪，确认无误再开刮。试算不动任何文件，也不跨境抓元数据
        </p>
      </div>

      <div class="space-y-2">
        <label class="block">
          <span class="text-xs text-gray-400">影片文件或目录</span>
          <input
            v-model="form.path"
            class="input mt-1 w-full font-mono text-xs"
            placeholder="/downloads/av/ABS-001"
            @keyup.enter="doPreview"
          >
          <span class="mt-1 block text-[11px] text-gray-600">
            容器内的路径。目录会递归扫，按番号分组，分集只抓一次元数据
          </span>
        </label>

        <label class="block">
          <span class="text-xs text-gray-400">硬链接目录（可选）</span>
          <input
            v-model="form.target_dir"
            class="input mt-1 w-full font-mono text-xs"
            placeholder="留空用设置里的「刮削输出目录」"
          >
          <span class="mt-1 block text-[11px] text-gray-600">
            只对本次生效。硬链接建不了跨文件系统的，填别的盘会在开刮前被拦下
          </span>
        </label>

        <label class="block">
          <span class="text-xs text-gray-400">指定番号（可选）</span>
          <input
            v-model="form.code"
            class="input mt-1 w-full font-mono text-xs"
            placeholder="ABS-001"
          >
          <span class="mt-1 block text-[11px] text-gray-600">
            文件名认不出番号时用。传目录时只认领认不出的那些，已认出的不动；
            认领到多个文件会按顺序编成 CD1／CD2
          </span>
        </label>

        <label class="flex items-center gap-2">
          <input v-model="form.fetch_meta" type="checkbox" class="accent-brand">
          <span class="text-xs text-gray-400">库里没有时去抓元数据</span>
          <span class="text-[11px] text-gray-600">关掉则只用本地已有的，省跨境请求</span>
        </label>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <button class="btn-ghost px-3 py-1.5 text-xs" :disabled="previewing" @click="doPreview">
          {{ previewing ? '试算中…' : '试算' }}
        </button>
        <button
          class="btn px-3 py-1.5 text-xs"
          :class="canRun ? 'bg-brand text-white' : 'btn-ghost opacity-50'"
          :disabled="!canRun"
          @click="doRun"
        >
          {{ running ? '刮削中…' : '开始刮削' }}
        </button>
        <span v-if="!items.length" class="text-[11px] text-gray-600">
          先试算，确认产物路径后才能开刮
        </span>
      </div>
    </div>

    <!-- 试算结果 -->
    <div v-if="result" class="card space-y-3">
      <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <p class="text-sm font-medium text-gray-300">试算结果</p>
        <p class="text-xs text-gray-400">
          <span class="text-brand">{{ items.length }}</span> 个产物
          <template v-if="trailerCount">
            · <span class="text-gray-500">{{ trailerCount }} 个预告片会跳过</span>
          </template>
          <template v-if="noMetaCount">
            · <span class="text-amber-400">{{ noMetaCount }} 个本地无元数据</span>
          </template>
        </p>
      </div>

      <p v-if="noMetaCount" class="text-[11px] text-gray-600">
        「本地无元数据」不代表刮不了 —— 试算不联网，开刮时会去抓。
        标题、演员、封面要抓到才有
      </p>

      <div v-if="items.length" class="overflow-x-auto">
        <table class="w-full text-xs">
          <thead class="text-gray-500">
            <tr class="border-b border-gray-800">
              <th class="py-1.5 pr-3 text-left font-normal">番号</th>
              <th class="py-1.5 pr-3 text-left font-normal">源文件</th>
              <th class="py-1.5 pr-3 text-left font-normal">产物路径</th>
              <th class="py-1.5 text-left font-normal">标记</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(item, index) in items"
              :key="index"
              class="border-b border-gray-800/50"
              :class="item.trailer ? 'opacity-40' : ''"
            >
              <td class="py-1.5 pr-3 align-top">
                <span class="font-mono text-brand">{{ item.code }}</span>
                <span v-if="item.part" class="ml-1 text-gray-500">CD{{ item.part }}</span>
              </td>
              <td class="max-w-[16rem] break-all py-1.5 pr-3 align-top font-mono text-[11px] text-gray-500">
                {{ item.source }}
              </td>
              <td class="max-w-[20rem] break-all py-1.5 pr-3 align-top font-mono text-[11px] text-gray-300">
                {{ item.target }}
              </td>
              <td class="py-1.5 align-top">
                <span v-if="item.trailer" class="text-gray-500">预告片·跳过</span>
                <span v-if="item.subbed" class="text-emerald-400">中文字幕</span>
                <span v-if="item.uncensored" class="ml-1 text-amber-400">无码</span>
                <span v-if="!item.has_meta" class="ml-1 text-gray-600">无元数据</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <EmptyState v-else text="这个路径下没有可刮削的影片" />

      <!-- 认不出番号的：给出可操作的下一步，而不是只说失败 -->
      <div v-if="unknown.length" class="border-t border-gray-800 pt-2">
        <p class="text-xs text-amber-400">
          {{ unknown.length }} 个文件认不出番号
        </p>
        <p class="mt-0.5 text-[11px] text-gray-600">
          在上面的「指定番号」里填一个，再试算即可认领。
          标着「欧美片」的本来就没有番号体系，属正常
        </p>
        <ul class="mt-1 space-y-0.5">
          <li
            v-for="(row, index) in unknown"
            :key="index"
            class="break-all font-mono text-[11px] text-gray-500"
          >
            {{ row.path }}
            <span v-if="row.western" class="ml-1 font-sans text-gray-600">（欧美片）</span>
          </li>
        </ul>
      </div>

      <p class="text-[11px] text-gray-600">
        目录模板 <span class="font-mono text-gray-500">{{ result.dir_template || '（留空·平铺）' }}</span>
        · 文件名模板 <span class="font-mono text-gray-500">{{ result.file_template }}</span>
      </p>
    </div>

    <!-- 刮削结果 -->
    <div v-if="runResult" class="card space-y-2">
      <p class="text-sm font-medium text-gray-300">刮削结果</p>
      <p class="text-xs text-gray-400">
        {{ runResult.total }} 个番号，
        <span class="text-emerald-400">{{ runResult.ok }}</span> 个成功
      </p>
      <div class="space-y-1">
        <div
          v-for="(row, index) in runResult.results || []"
          :key="index"
          class="border-b border-gray-800/50 pb-1 last:border-0"
        >
          <p class="text-xs">
            <span class="font-mono" :class="row.ok ? 'text-brand' : 'text-gray-500'">
              {{ row.code || '—' }}
            </span>
            <span class="ml-2 text-gray-500">
              产物 {{ row.links.length }} · NFO {{ row.nfo_written }} · 图片 {{ row.images_written }}
            </span>
          </p>
          <p v-if="row.error" class="mt-0.5 text-[11px] text-red-400">{{ row.error }}</p>
          <p
            v-for="(note, i) in row.skipped || []"
            :key="i"
            class="mt-0.5 break-all text-[11px] text-gray-600"
          >
            {{ note }}
          </p>
        </div>
      </div>
    </div>

    <!-- 模板字段速查 -->
    <div class="card space-y-2">
      <button
        class="flex w-full items-center justify-between text-left"
        @click="showFields = !showFields"
      >
        <span class="text-sm font-medium text-gray-300">命名模板字段</span>
        <span class="text-xs text-gray-500">{{ showFields ? '收起' : '展开' }}</span>
      </button>

      <template v-if="showFields">
        <p class="text-[11px] text-gray-600">
          基础语法 <span class="font-mono text-gray-500">{{ SYNTAX_BASIC }}</span>，缺失填「未知」；
          高级语法 <span class="font-mono text-gray-500">{{ SYNTAX_JINJA }}</span>，
          缺失为空并支持条件与 filter。写法与 MDCng 一致，模板可直接搬过来。
          改模板去设置 → 刮削
        </p>
        <p v-if="!jinjaAvailable" class="text-[11px] text-amber-400">
          未安装 jinja2，高级语法的条件与 filter 不可用，只做变量替换
        </p>
        <div class="grid grid-cols-2 gap-x-4 gap-y-0.5 sm:grid-cols-3">
          <p v-for="f in fields" :key="f.name" class="text-[11px]">
            <span class="font-mono text-gray-400">{{ f.name }}</span>
            <span class="ml-1 text-gray-600">{{ f.desc }}</span>
          </p>
        </div>
      </template>
    </div>
  </div>
</template>
