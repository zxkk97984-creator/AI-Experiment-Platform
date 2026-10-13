<script setup>
// 教师只读练习排行榜（批次 6）：全平台榜 / 班内榜 × 普通测试榜 / AI 综合榜。
// 只读页面：复用学生的榜单组件但传 readonly，不渲染 opt-in 开关，也不提供任何写操作；
// 教师的练习写接口在后端一律 403。六态（加载/空/失败/未 opt-in/权限不足/未选班级）
// 由共用组件统一覆盖。
import AppLayout from '../../components/layout/AppLayout.vue'
import PracticeNotice from '../../components/practice/PracticeNotice.vue'
import PracticeScopeBoard from '../../components/practice/PracticeScopeBoard.vue'
</script>

<template>
  <AppLayout content-mode="list">
    <section class="practice-leaderboard">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">PRACTICE BOARD</p>
          <h1>练习排行榜</h1>
          <p class="lead subtitle">只读视图：可查看任课班级与全平台榜单，不做任何写操作</p>
        </div>
      </section>

      <PracticeNotice tone="info" title="只读入口">
        排行榜只展示聚合名次与化名：教师无练习写权限，也看不到学生源码、学号或他人评分详情。
      </PracticeNotice>

      <PracticeScopeBoard readonly />
    </section>
  </AppLayout>
</template>

<style scoped>
.practice-leaderboard { display: flex; flex-direction: column; gap: var(--space-4); }
</style>
