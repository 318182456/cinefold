<script setup>
const props = defineProps({ modelValue: { type: Object, required: true } })
const emit = defineEmits(['update:modelValue'])
function set(key, value) { emit('update:modelValue', { ...props.modelValue, [key]: value }) }
const fields = [
  ['prefixes', '番号前缀', 'SONE, IPX'], ['publisher', '发行商', '精确名称'],
  ['producer', '制作商', '精确名称'], ['series', '系列', '精确名称'],
  ['genres', '标签', '多个用逗号分隔'], ['casts', '演员', '多个用逗号分隔'],
]
</script>
<template>
  <div class="grid gap-3 sm:grid-cols-2">
    <label v-for="[key, label, placeholder] in fields" :key="key"><span class="label">{{ label }}</span>
      <input class="input" :value="Array.isArray(modelValue[key]) ? modelValue[key].join(', ') : modelValue[key] || ''" :placeholder="placeholder" @input="set(key, $event.target.value)">
    </label>
    <label><span class="label">标签条件</span><select class="input" :value="modelValue.genre_mode || 'all'" @change="set('genre_mode', $event.target.value)"><option value="all">全部满足</option><option value="any">满足任一</option></select></label>
    <label><span class="label">最早发行日期</span><input class="input" type="date" :value="modelValue.earliest_release_date || ''" @input="set('earliest_release_date', $event.target.value)"></label>
    <label><span class="label">出演人数上限</span><input class="input" type="number" min="1" :value="modelValue.max_coactors || ''" @input="set('max_coactors', $event.target.value ? Number($event.target.value) : null)"></label>
    <label class="flex items-center gap-2 text-sm"><input type="checkbox" :checked="modelValue.exclude_vr" @change="set('exclude_vr', $event.target.checked)">排除 VR</label>
  </div>
</template>
