<script setup>
defineProps({ name: String, size: { type: [Number, String], default: 20 } })
const paths = {
  search: 'M21 21l-5-5m2-6a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  arrow: 'M5 12h14m-5-5 5 5-5 5',
  chevron: 'm9 5 7 7-7 7',
  refresh: 'M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 2M5 16a8 8 0 0 0 13 2',
  grid: 'M3 3h7v7H3zm11 0h7v7h-7zM3 14h7v7H3zm11 0h7v7h-7z',
  library: 'M4 4h5v16H4zm5 0h5v16H9zm7 1 4-1 3 15-4 1z',
  download: 'M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4',
  clock: 'M12 8v5l3 2m6-3a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  task: 'M9 5H5v16h14V5h-4M9 3h6v4H9zm0 11 2 2 4-4',
  menu: 'M4 6h16M4 12h16M4 18h16',
  close: 'm6 6 12 12M6 18 18 6',
  user: 'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0M4 21a8 8 0 0 1 16 0',
  logout: 'M9 4H4v16h5m5-13 5 5-5 5m-6-5h11',
  play: 'm9 5 11 7-11 7z',
  film: 'M4 3h16v18H4zM8 3v18M16 3v18M4 8h4m8 0h4M4 16h4m8 0h4',
  check: 'm5 12 4 4L19 6',
  bookmark: 'M6 3h12v18l-6-4-6 4z',
}
</script>

<template>
  <svg :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" class="shrink-0">
    <path :d="paths[name] || paths.grid" />
  </svg>
</template>
