<script setup>
defineProps({ classes: { type: Array, default: () => [] } })
</script>

<template>
  <details v-if="classes.length" class="class-summary">
    <summary :title="classes.map(item => item.name).join('、')">
      {{ classes[0].code || classes[0].name }}<span v-if="classes.length > 1"> 等 {{ classes.length }} 个班</span>
    </summary>
    <ul><li v-for="item in classes" :key="item.id || item.name">{{ item.name }}</li></ul>
  </details>
  <span v-else class="class-summary-empty">未设置班级</span>
</template>

<style scoped>
.class-summary { min-width: 0; font-size: var(--text-sm); }
summary { cursor: pointer; overflow-wrap: anywhere; color: var(--muted); }
summary::marker { color: var(--accent); }
ul { margin: 8px 0 0; padding-left: 16px; white-space: normal; overflow-wrap: anywhere; }
li + li { margin-top: 4px; }
.class-summary-empty { color: var(--muted); font-size: var(--text-sm); }
</style>
