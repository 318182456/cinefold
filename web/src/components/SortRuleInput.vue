<script setup>
import { computed } from 'vue'

// 排序键与它的含义。与 app/utils/filters.py 的 sort_torrents 保持一致
const KEYS = [
  { k: 'free', label: '免费种', desc: '不计下载量的种子' },
  { k: 'chinese', label: '中文字幕', desc: '标题含中文/中字/字幕等' },
  { k: 'uc', label: '无码破解', desc: '标题含无码/破解/流出等' },
  { k: 'uhd', label: '4K 超清', desc: '标题含 4K/2160p 等' },
  { k: 'vr', label: 'VR', desc: 'VR 片源' },
  { k: 'seeders', label: '做种数', desc: '做种人数越多越靠前' },
  { k: 'size', label: '体积', desc: '文件越大越靠前' },
  { k: 'site', label: '站点优先级', desc: '按「优先站点」的顺序' },
]

const LABELS = Object.fromEntries(KEYS.map((item) => [item.k, item.label]))
const DESCS = Object.fromEntries(KEYS.map((item) => [item.k, item.desc]))

const model = defineModel({ type: String, default: '' })

// "free,!uhd" → [{ k:'free', desc:false }, { k:'uhd', desc:true }]
const rules = computed(() =>
  (model.value || '')
    .split(',')
    .map((raw) => raw.trim())
    .filter(Boolean)
    .map((raw) => ({ k: raw.replace(/^!/, ''), negate: raw.startsWith('!') }))
    // 不认识的键直接丢掉，避免界面上出现空行
    .filter((item) => item.k in LABELS),
)

const unused = computed(() => KEYS.filter((item) => !rules.value.some((r) => r.k === item.k)))

function write(list) {
  model.value = list.map((item) => (item.negate ? `!${item.k}` : item.k)).join(',')
}

function add(key) {
  if (!key) return
  write([...rules.value, { k: key, negate: false }])
}

function remove(index) {
  write(rules.value.filter((_, i) => i !== index))
}

function move(index, delta) {
  const list = [...rules.value]
  const target = index + delta
  if (target < 0 || target >= list.length) return
  ;[list[index], list[target]] = [list[target], list[index]]
  write(list)
}

function toggle(index) {
  write(rules.value.map((item, i) => (i === index ? { ...item, negate: !item.negate } : item)))
}
</script>

<template>
  <div class="space-y-2">
    <p class="text-[11px] text-gray-600">越靠上优先级越高</p>

    <div v-if="rules.length" class="space-y-1.5">
      <div
        v-for="(rule, index) in rules"
        :key="rule.k"
        class="flex items-center gap-2 rounded-lg border border-gray-800 bg-gray-900/60 px-2.5 py-1.5"
      >
        <span class="w-5 text-center text-[11px] tabular-nums text-gray-600">
          {{ index + 1 }}
        </span>

        <div class="min-w-0 flex-1">
          <p class="truncate text-sm text-gray-300">{{ LABELS[rule.k] }}</p>
          <p class="truncate text-[11px] text-gray-600">{{ DESCS[rule.k] }}</p>
        </div>

        <!-- 降权：希望该属性为假，如「非 4K 优先」 -->
        <button
          class="btn-ghost shrink-0 px-2 py-0.5 text-[11px]"
          :class="rule.negate ? 'border-amber-700 text-amber-300' : ''"
          :title="rule.negate ? '降权：该属性为假的排前面' : '加权：该属性为真的排前面'"
          @click="toggle(index)"
        >
          {{ rule.negate ? '反向' : '正向' }}
        </button>

        <button
          class="btn-ghost shrink-0 px-2 py-0.5 text-[11px]"
          :disabled="index === 0"
          title="上移"
          @click="move(index, -1)"
        >
          ↑
        </button>
        <button
          class="btn-ghost shrink-0 px-2 py-0.5 text-[11px]"
          :disabled="index === rules.length - 1"
          title="下移"
          @click="move(index, 1)"
        >
          ↓
        </button>
        <button
          class="btn-ghost shrink-0 px-2 py-0.5 text-[11px] text-gray-500"
          title="移除"
          @click="remove(index)"
        >
          ✕
        </button>
      </div>
    </div>

    <p v-else class="text-xs text-gray-500">未设置排序规则，将按搜索结果原顺序</p>

    <select
      v-if="unused.length"
      class="input text-sm"
      value=""
      @change="add($event.target.value); $event.target.value = ''"
    >
      <option value="">添加排序条件…</option>
      <option v-for="item in unused" :key="item.k" :value="item.k">
        {{ item.label }} — {{ item.desc }}
      </option>
    </select>
  </div>
</template>
