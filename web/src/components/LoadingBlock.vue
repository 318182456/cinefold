<script setup>
defineProps({ rows: { type: Number, default: 3 } })
</script>

<template>
  <div class="space-y-3">
    <div v-for="i in rows" :key="i" class="animate-pulse rounded-lg bg-gray-800/60" style="height: 72px" />
  </div>
</template>
