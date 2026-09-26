<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import { statisticsAPI } from '../../api/statistics.js'
import { useAppStore } from '../../stores/app.js'

const router = useRouter()
const app = useAppStore()
const data = ref(null)
const loading = ref(true)

const averageText = computed(() => (data.value?.average_score == null ? '—' : data.value.average_score.toFixed(1)))

async function load() {
  loading.value = true
  try {
    const res = await statisticsAPI.teacherGrades()
    data.value = res.data
  } catch {
    app.showToast('加载成绩统计失败', 'error')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <AppLayout content-mode="list">
    <section class="stats-page">
      <section class="page-head">
        <div class="ph-title">
          <p class="eyebrow">评分 / 统计</p>
          <h1>成绩统计</h1>
          <p class="lead">跨课程查看考试参与、平均分与及格率。</p>
        </div>
      </section>

      <section class="metric-strip" aria-label="成绩总览">
        <div class="metric"><span class="m-value">{{ data?.course_count ?? '—' }}</span><span class="m-label">全部课程</span></div>
        <div class="metric"><span class="m-value">{{ data?.active_course_count ?? '—' }}</span><span class="m-label">进行中课程</span></div>
        <div class="metric"><span class="m-value">{{ data?.exam_count ?? '—' }}</span><span class="m-label">考试场次</span></div>
        <div class="metric"><span class="m-value">{{ averageText }}</span><span class="m-label">平均分</span></div>
        <div class="metric em"><span class="m-value">{{ data?.pass_rate ?? '—' }}%</span><span class="m-label">及格率</span></div>
      </section>

      <section class="table-wrap">
        <div class="panel-head"><div class="ph-label"><p class="eyebrow">Exams</p><h3>考试列表</h3></div></div>
        <div v-if="loading" class="panel-body">加载中…</div>
        <div v-else-if="!data?.exams?.length" class="empty">
          <div class="empty-mark"><AppIcon name="exam" :size="20" /></div>
          <h3>暂无考试</h3>
          <p>创建并发布考试后，成绩会汇总到这里。</p>
        </div>
        <div v-else class="table-scroll">
          <table class="ds-table">
            <thead>
              <tr><th>考试</th><th>课程</th><th>状态</th><th class="cell-num">应考</th><th class="cell-num">已评分</th><th class="cell-num">平均分</th><th class="cell-num">及格率</th><th class="col-actions">操作</th></tr>
            </thead>
            <tbody>
              <tr v-for="exam in data.exams" :key="exam.id">
                <td><button type="button" class="cell-main course-link" @click="router.push(exam.route)">{{ exam.title }}</button></td>
                <td>{{ exam.course_title || '—' }}</td>
                <td>
                  <span class="badge" :class="exam.status === 'published' ? 'badge-success' : 'badge-neutral'"><span class="dot"></span>{{ exam.status === 'published' ? '已发布' : exam.status }}</span>
                </td>
                <td class="cell-num">{{ exam.expected_count }}</td>
                <td class="cell-num">{{ exam.graded_count }}</td>
                <td class="cell-num">{{ exam.average_score ?? '—' }}</td>
                <td class="cell-num">{{ exam.pass_rate }}%</td>
                <td class="col-actions"><button type="button" class="btn btn-ghost btn-sm" @click="router.push(exam.route)">成绩详情</button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </section>
  </AppLayout>
</template>

<style scoped>
.stats-page { display: flex; flex-direction: column; gap: var(--space-5); }
.course-link { padding: 0; border: 0; background: transparent; font-weight: 600; color: var(--fg); }
.course-link:hover { color: var(--accent); background: transparent; }
</style>
