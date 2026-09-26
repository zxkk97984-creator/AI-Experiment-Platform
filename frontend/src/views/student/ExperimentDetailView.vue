<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import NotebookPlayer from '../../components/notebook/NotebookPlayer.vue'

const route = useRoute()
const moduleId = computed(() => route.params.id)
</script>

<template>
  <AppLayout content-mode="workspace">
    <NotebookPlayer mode="module" :entry-id="moduleId" />
  </AppLayout>
</template>
