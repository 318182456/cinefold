<script setup>
/**
 * 站点标识。
 *
 * byte + muse：底层是字节（方块网格），上层是一段律动的波形。
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
    aria-label="byte-muse"
  >
    <rect width="32" height="32" rx="8" class="fill-brand" />

    <!-- 字节网格：左下角三个方块，暗示二进制 -->
    <g class="fill-white/25">
      <rect x="7" y="20" width="3.5" height="3.5" rx="1" />
      <rect x="12" y="20" width="3.5" height="3.5" rx="1" />
      <rect x="7" y="15" width="3.5" height="3.5" rx="1" />
    </g>

    <!-- 波形：从左下升到右上，收在一个实心点上 -->
    <path
      d="M7 19.5C10 19.5 11 9 14.5 9C18 9 18.5 22 22 22C24 22 25 16.5 26 16.5"
      stroke="white"
      stroke-width="2.4"
      stroke-linecap="round"
      stroke-linejoin="round"
    />
    <circle cx="26" cy="16.5" r="2.2" fill="white" />
  </svg>
</template>
