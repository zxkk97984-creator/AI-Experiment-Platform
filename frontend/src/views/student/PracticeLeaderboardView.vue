<script setup>
// 学生练习排行榜（批次 6）：全平台榜 / 班内榜 × 普通测试榜 / AI 综合榜，
// 支持「只看我的名次（前后各 3 名）」与全平台榜主动加入/退出。
// 榜单只展示聚合与化名：不展示学号、源码或他人评分详情，也不做末位展示。
import { useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import PracticeScopeBoard from '../../components/practice/PracticeScopeBoard.vue'

const router = useRouter()
</script>

<template>
  <AppLayout content-mode="list">
    <section class="practice-board">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">PRACTICE BOARD</p>
          <h1>练习排行榜</h1>
          <p class="lead subtitle">练习成绩与排名不进入课程总评；全平台榜以化名展示，可随时退出</p>
        </div>
        <div class="ph-actions">
          <button type="button" class="btn btn-secondary" @click="router.push('/student/practice')">
            <AppIcon name="arrow-right" :size="14" />去练习中心
          </button>
        </div>
      </section>
      <PracticeScopeBoard show-opt-in />
    </section>
  </AppLayout>
</template>

<style scoped>
.practice-board { display: flex; flex-direction: column; gap: var(--space-4); }
</style>
