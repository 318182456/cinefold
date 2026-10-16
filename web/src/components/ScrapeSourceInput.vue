<script setup>
import { computed } from 'vue'
const props = defineProps({ modelValue: { type: [String, Array], default: '' } })
const emit = defineEmits(['update:modelValue'])
const sources = ['javbus', 'javdb', 'airav', 'avbase', 'avmoo', 'avsox', 'carib', 'dmm', 'mgstage', 'fc2', 'fc2hub', 'jav321', 'missav', '7mmtv', 'hbox', 'madou', 'madouqu', 'xchina', 'theporndb']
const selected = computed(() => Array.isArray(props.modelValue) ? props.modelValue : (props.modelValue || '').split(',').filter(Boolean))
function commit(items) { emit('update:modelValue', items.join(',')) }
function move(index, step) {
  const items = [...selected.value]
  const next = index + step
  if (next < 0 || next >= items.length) return
  ;[items[index], items[next]] = [items[next], items[index]]
  commit(items)
}
</script>

<template>
  <div class="space-y-2">
    <div v-if="selected.length" class="flex flex-wrap gap-1.5">
      <span v-for="(site, i) in selected" :key="site" class="inline-flex items-center gap-1 rounded bg-gray-800 px-2 py-1 text-xs text-gray-200">
        {{ i + 1 }}. {{ site }}
        <button type="button" :disabled="i === 0" class="disabled:opacity-20" :aria-label="`提高 ${site} 优先级`" @click="move(i, -1)">←</button>
        <button type="button" :disabled="i === selected.length - 1" class="disabled:opacity-20" :aria-label="`降低 ${site} 优先级`" @click="move(i, 1)">→</button>
        <button type="button" class="text-gray-500 hover:text-red-400" :aria-label="`移除 ${site}`" @click="commit(selected.filter(x => x !== site))">×</button>
      </span>
    </div>
    <select class="input" aria-label="添加刮削源" value="" @change="commit([...selected, $event.target.value]); $event.target.value = ''">
      <option value="" disabled>{{ selected.length ? '添加来源' : '未配置，沿用默认规则' }}</option>
      <option v-for="site in sources.filter(x => !selected.includes(x))" :key="site" :value="site">{{ site }}</option>
    </select>
  </div>
</template>
