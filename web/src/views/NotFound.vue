<script setup>
import { useRouter } from 'vue-router'
const router = useRouter()
</script>

<template>
  <div class="flex min-h-[60vh] flex-col items-center justify-center gap-4 px-4 lg:h-full lg:min-h-0">
    <p class="text-5xl font-semibold text-gray-700">404</p>
    <p class="text-sm text-gray-500">找不到页面</p>
    <button class="btn-primary" @click="router.push('/')">返回首页</button>
  </div>
</template>
