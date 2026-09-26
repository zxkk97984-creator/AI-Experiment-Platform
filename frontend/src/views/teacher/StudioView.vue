<script setup>
import AppLayout from '../../components/layout/AppLayout.vue'
import StudioEditor from '../../components/notebook/StudioEditor.vue'
import { useRoute } from 'vue-router'
const route = useRoute()
const templateId = route.params.lid
</script>

<template>
  <AppLayout content-mode="workspace">
    <StudioEditor :template-id="templateId" />
  </AppLayout>
</template>
