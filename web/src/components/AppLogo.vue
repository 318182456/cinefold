<script setup>
/**
 * 站点标识。
 *
 * cinefold：胶片的两页折叠为一份收藏。
 * 用 currentColor 取色，跟随外层文字颜色，深浅色主题都不用改。
 */
defineProps({
  size: { type: [Number, String], default: 32 },
})
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 32 32"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    role="img"
    aria-label="cinefold"
  >
    <rect width="32" height="32" rx="8" class="fill-brand" />

    <path d="M8 8h13v17L8 22V8Z" fill="#ffffff" />
    <path d="m21 8 5 4v10l-5 3V8Z" fill="#ffffff" opacity=".6" />
    <path d="M11 11h2m-2 4h2m-2 4h2m4-8h1m-1 4h1m-1 4h1" stroke="#197653" stroke-width="1.5" stroke-linecap="round" />
  </svg>
</template>
