<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import ModalDialog from '@/components/ModalDialog.vue'
const router = useRouter(), open = ref(false), keyword = ref('')
const destinations = computed(() => router.getRoutes().filter(r => r.meta.title && r.name).filter(r => `${r.meta.title} ${r.name}`.toLowerCase().includes(keyword.value.toLowerCase())))
function keydown(event) { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); keyword.value = ''; open.value = !open.value } }
function go(name) { open.value = false; router.push({ name }) }
onMounted(() => window.addEventListener('keydown', keydown))
onUnmounted(() => window.removeEventListener('keydown', keydown))
</script>
<template>
  <button class="btn-ghost !px-2 !py-1 text-xs" title="快捷导航 Ctrl+K / ⌘K" @click="open = true">快捷导航 <kbd class="hidden text-gray-500 sm:inline">⌘ / Ctrl K</kbd></button>
  <ModalDialog v-model:open="open" title="快捷导航" scrollable><input v-model="keyword" class="input mb-3" placeholder="搜索页面" aria-label="搜索页面"><div class="space-y-1"><button v-for="r in destinations" :key="r.name" class="block w-full rounded-lg px-3 py-2 text-left text-sm hover:bg-gray-800" @click="go(r.name)">{{ r.meta.title }}</button></div></ModalDialog>
</template>
