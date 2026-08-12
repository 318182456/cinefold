<script setup>
// 番号卡片列表的多选工具条，配合 useCodeSelection 使用
defineProps({
  sel: { type: Object, required: true },
  // 'cancel' 用在订阅列表页，'subscribe' 用在榜单/厂牌/看板这类发现页
  action: { type: String, default: 'subscribe' },
})
</script>

<template>
  <div class="flex flex-wrap items-center gap-2">
    <button v-if="!sel.active.value" class="btn-ghost px-3 py-1 text-xs" @click="sel.enter()">
      多选
    </button>

    <template v-else>
      <button class="btn-ghost px-3 py-1 text-xs" @click="sel.toggleAll()">
        {{ sel.allSelected.value ? '取消本页全选' : '本页全选' }}
      </button>
      <span class="text-xs tabular-nums text-gray-500">已选 {{ sel.count.value }}</span>

      <button
        v-if="action === 'subscribe'"
        class="btn-primary px-3 py-1 text-xs"
        :disabled="!sel.count.value || sel.busy.value"
        @click="sel.subscribeSelected()"
      >
        批量订阅
      </button>
      <button
        v-else
        class="btn px-3 py-1 text-xs"
        :class="!sel.count.value || sel.busy.value ? 'btn-ghost' : 'bg-red-900 text-red-200'"
        :disabled="!sel.count.value || sel.busy.value"
        @click="sel.cancelSelected()"
      >
        批量取消订阅
      </button>

      <button class="btn-ghost px-3 py-1 text-xs" :disabled="sel.busy.value" @click="sel.exit()">
        退出多选
      </button>
    </template>
  </div>
</template>
