<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import ModalDialog from '@/components/ModalDialog.vue'
import AppIcon from '@/components/AppIcon.vue'
const router = useRouter(), open = ref(false), keyword = ref('')
const searchInput = ref(null), trigger = ref(null)
watch(open, async value => { await nextTick(); if (value) searchInput.value?.focus(); else trigger.value?.focus() })
const destinations = computed(() => router.getRoutes().filter(r => r.meta.title && r.name).filter(r => `${r.meta.title} ${r.name}`.toLowerCase().includes(keyword.value.toLowerCase())))
function keydown(event) { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); keyword.value = ''; open.value = !open.value } }
function go(name) { open.value = false; router.push({ name }) }
onMounted(() => window.addEventListener('keydown', keydown))
onUnmounted(() => window.removeEventListener('keydown', keydown))
</script>
<template>
  <button ref="trigger" class="command-trigger" aria-label="快捷导航" title="快捷导航 Ctrl+K / ⌘K" @click="keyword = ''; open = true"><AppIcon name="search" :size="16" /><span class="hidden sm:inline">跳转到页面…</span><kbd class="ml-4 hidden text-[10px] text-gray-500 md:inline">Ctrl K</kbd></button>
  <ModalDialog v-model:open="open" title="快捷导航" scrollable><input ref="searchInput" v-model="keyword" class="input mb-3" placeholder="搜索页面，如：下载、任务、设置" aria-label="搜索页面"><div class="space-y-1"><button v-for="r in destinations" :key="r.name" class="flex w-full items-center justify-between rounded-lg px-3 py-2.5 text-left text-sm hover:bg-gray-800" @click="go(r.name)">{{ r.meta.title }}<AppIcon name="arrow" :size="14" class="text-gray-500" /></button><p v-if="!destinations.length" class="py-6 text-center text-xs text-gray-500">没有匹配的页面，试试其他名称。</p></div></ModalDialog>
</template>
