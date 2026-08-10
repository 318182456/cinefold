<script setup>
/**
 * 标签选择器。对外仍是逗号分隔的字符串，与后端配置格式保持一致。
 *
 * 番号前缀这类配置手输逗号容易漏写或写全角，这里把它拆成可视化标签。
 */
import { computed, ref } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  placeholder: { type: String, default: '输入后回车添加' },
  // 建议项，点击即添加
  suggestions: { type: Array, default: () => [] },
  // 标签统一转大写，番号前缀用
  uppercase: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue'])

const draft = ref('')

const tags = computed(() =>
  (props.modelValue || '')
    .split(/[,|\s]+/)
    .map((item) => item.trim())
    .filter(Boolean),
)

const unusedSuggestions = computed(() =>
  props.suggestions.filter((item) => !tags.value.includes(normalize(item))),
)

function normalize(raw) {
  const value = String(raw || '').trim()
  return props.uppercase ? value.toUpperCase() : value
}

function commit(list) {
  emit('update:modelValue', list.join(','))
}

function add(raw) {
  // 一次粘贴多个的情况，按分隔符拆开
  const incoming = String(raw || '')
    .split(/[,|、\s]+/)
    .map(normalize)
    .filter(Boolean)

  const next = [...tags.value]
  incoming.forEach((item) => {
    if (!next.includes(item)) next.push(item)
  })
  commit(next)
  draft.value = ''
}

function remove(tag) {
  commit(tags.value.filter((item) => item !== tag))
}

function onEnter() {
  if (draft.value.trim()) add(draft.value)
}

/** 输入框为空时退格，删掉最后一个标签 */
function onBackspace() {
  if (!draft.value && tags.value.length) remove(tags.value[tags.value.length - 1])
}
</script>

<template>
  <div>
    <div
      class="flex min-h-[38px] w-full flex-wrap items-center gap-1.5 rounded-lg border
             border-gray-700 bg-gray-900 px-2 py-1.5
             focus-within:border-brand focus-within:ring-1 focus-within:ring-brand"
    >
      <span
        v-for="tag in tags"
        :key="tag"
        class="inline-flex items-center gap-1 rounded bg-gray-800 px-2 py-0.5
               text-xs text-gray-200"
      >
        {{ tag }}
        <button
          type="button"
          class="text-gray-500 hover:text-red-400"
          :aria-label="`移除 ${tag}`"
          @click="remove(tag)"
        >
          ×
        </button>
      </span>

      <input
        v-model="draft"
        type="text"
        class="min-w-[8rem] flex-1 bg-transparent text-sm text-gray-100
               placeholder-gray-500 outline-none"
        :placeholder="tags.length ? '' : placeholder"
        @keydown.enter.prevent="onEnter"
        @keydown.,.prevent="onEnter"
        @keydown.backspace="onBackspace"
        @blur="onEnter"
      />
    </div>

    <div v-if="unusedSuggestions.length" class="mt-1.5 flex flex-wrap gap-1">
      <button
        v-for="item in unusedSuggestions"
        :key="item"
        type="button"
        class="rounded border border-gray-700 px-1.5 py-0.5 text-[11px]
               text-gray-500 hover:border-brand hover:text-gray-300"
        @click="add(item)"
      >
        + {{ item }}
      </button>
    </div>
  </div>
</template>
