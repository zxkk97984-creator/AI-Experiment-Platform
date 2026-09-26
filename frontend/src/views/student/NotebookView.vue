<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import NotebookPlayer from '../../components/notebook/NotebookPlayer.vue'

const route = useRoute()
const courseId = computed(() => route.params.id)
const lessonId = computed(() => route.params.lid)
</script>

<template>
  <AppLayout content-mode="workspace">
    <NotebookPlayer mode="lesson" :entry-id="lessonId" :course-id="courseId" />
  </AppLayout>
</template>
