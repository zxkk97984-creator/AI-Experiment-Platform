<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import StudioEditor from '../../components/notebook/StudioEditor.vue'

const route = useRoute()
const templateId = computed(() => route.params.lid)
const prefix = computed(() => (route.path.startsWith('/admin') ? '/admin' : '/teacher'))
const backTo = computed(() => `${prefix.value}/experiments`)
</script>

<template>
  <AppLayout content-mode="workspace">
    <StudioEditor :template-id="templateId" :back-to="backTo" />
  </AppLayout>
</template>
