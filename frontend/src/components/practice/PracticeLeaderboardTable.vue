<script setup>
// 只读排行榜表格：教师页与管理端复用。全平台榜为匿名化名，不展示学号与源码。
import { formatDateTime } from '../../utils/format.js'

defineProps({
  items: { type: Array, default: () => [] },
})
</script>

<template>
  <div class="table-wrap leaderboard">
    <div class="table-scroll">
      <table class="ds-table">
        <caption class="sr-only">练习排行榜（只读）</caption>
        <thead>
          <tr>
            <th scope="col">名次</th>
            <th scope="col">学生</th>
            <th scope="col">通过题数</th>
            <th scope="col">总分</th>
            <th scope="col">平均分</th>
            <th scope="col">达成时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in items" :key="`${row.rank}-${row.display_name}`">
            <td class="rank mono">{{ row.rank }}</td>
            <td>
              <strong>{{ row.display_name }}</strong>
              <small v-if="row.is_me" class="me-tag">我</small>
            </td>
            <td class="mono">{{ row.solved_count ?? 0 }}</td>
            <td class="mono score">{{ row.total_score ?? 0 }}</td>
            <td class="mono">{{ row.avg_score ?? '—' }}</td>
            <td class="time">{{ row.achieved_at ? formatDateTime(row.achieved_at) : '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.leaderboard { border-radius: var(--radius-lg); }
.rank { font-weight: 600; color: var(--muted); }
.score { color: var(--accent); font-weight: 600; }
.time { color: var(--muted); }
.me-tag { margin-left: var(--space-2); padding: 1px var(--space-2); border-radius: var(--radius-sm); background: var(--accent-soft); color: var(--accent); font-size: var(--text-xs); }
</style>
