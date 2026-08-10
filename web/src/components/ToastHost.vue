<script setup>
import { useToast } from '@/composables/useToast'

const { toasts } = useToast()

const styles = {
  success: 'border-emerald-600/50 bg-emerald-950/90 text-emerald-200',
  error: 'border-red-600/50 bg-red-950/90 text-red-200',
  info: 'border-gray-700 bg-gray-900/95 text-gray-200',
}
</script>

<template>
  <div class="pointer-events-none fixed inset-x-0 top-4 z-[100] flex flex-col items-center gap-2 px-4">
    <TransitionGroup
      enter-active-class="transition duration-200"
      enter-from-class="-translate-y-2 opacity-0"
      leave-active-class="transition duration-150"
      leave-to-class="opacity-0"
    >
      <div
        v-for="toast in toasts"
        :key="toast.id"
        class="pointer-events-auto max-w-md rounded-lg border px-4 py-2.5 text-sm shadow-lg backdrop-blur"
        :class="styles[toast.type]"
      >
        {{ toast.message }}
      </div>
    </TransitionGroup>
  </div>
</template>
