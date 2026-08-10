<script setup>
import { computed } from 'vue'
import TagInput from '@/components/TagInput.vue'

const props = defineProps({
  field: { type: Object, required: true },
  // 配置对象。嵌套字段（field.group）从它的子对象里取值
  form: { type: Object, required: true },
  // select 的动态选项，覆盖 field.options
  options: { type: Array, default: null },
})

const choices = computed(() => props.options || props.field.options || [])

const value = computed({
  get() {
    const { field, form } = props
    return field.group ? (form[field.group] || {})[field.k] : form[field.k]
  },
  set(next) {
    const { field, form } = props
    if (!field.group) {
      form[field.k] = next
      return
    }
    if (!form[field.group]) form[field.group] = {}
    form[field.group][field.k] = next
  },
})
</script>

<template>
  <div>
    <label class="label">{{ field.label }}</label>

    <label v-if="field.t === 'bool'" class="flex items-center gap-2">
      <input v-model="value" type="checkbox" class="h-4 w-4 accent-emerald-500" />
      <span class="text-sm text-gray-400">启用</span>
    </label>

    <select v-else-if="field.t === 'select'" v-model="value" class="input">
      <option v-for="option in choices" :key="option" :value="option">
        {{ option || '（默认）' }}
      </option>
    </select>

    <textarea
      v-else-if="field.t === 'textarea'"
      v-model="value"
      class="input font-mono text-xs"
      rows="3"
    />

    <TagInput
      v-else-if="field.t === 'tags'"
      v-model="value"
      :placeholder="field.ph || '输入后回车添加'"
      :suggestions="field.suggestions || []"
      :uppercase="!!field.upper"
    />

    <input
      v-else
      v-model="value"
      :type="field.t === 'password' ? 'password' : 'text'"
      class="input"
      :placeholder="field.ph || ''"
    />

    <p v-if="field.hint" class="mt-1 text-[11px] text-gray-600">{{ field.hint }}</p>
  </div>
</template>
