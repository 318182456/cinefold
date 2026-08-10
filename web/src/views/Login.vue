<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { login } from '@/api'
import { useToast } from '@/composables/useToast'

const route = useRoute()
const router = useRouter()
const toast = useToast()

const username = ref('')
const password = ref('')
const loading = ref(false)

async function submit() {
  if (!username.value || !password.value) {
    toast.error('请填写用户名和密码')
    return
  }
  loading.value = true
  try {
    const data = await login(username.value, password.value)
    localStorage.setItem('token', data.token)
    router.push(route.query.redirect || { name: 'Dashboard' })
  } catch (err) {
    toast.error(err.message)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="flex min-h-screen items-center justify-center px-4">
    <div class="w-full max-w-sm">
      <div class="mb-8 flex flex-col items-center gap-3">
        <div class="h-12 w-12 rounded-2xl bg-brand" />
        <h1 class="text-xl font-semibold tracking-tight">byte-muse</h1>
      </div>

      <form class="card space-y-4" @submit.prevent="submit">
        <div>
          <label class="label">用户名</label>
          <input v-model.trim="username" class="input" autocomplete="username" />
        </div>
        <div>
          <label class="label">密码</label>
          <input v-model="password" type="password" class="input" autocomplete="current-password" />
        </div>
        <button type="submit" class="btn-primary w-full" :disabled="loading">
          {{ loading ? '登录中…' : '登入' }}
        </button>
      </form>

      <p class="mt-4 text-center text-xs text-gray-600">
        忘记密码？删除数据目录下的 byte-muse.db 中 user 表记录后重启，会重新生成账号
      </p>
    </div>
  </div>
</template>
