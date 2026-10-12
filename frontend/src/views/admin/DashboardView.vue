<script setup>
import { useRouter } from 'vue-router'
import AppLayout from '../../components/layout/AppLayout.vue'
import AppIcon from '../../components/ui/AppIcon.vue'
import { useAuthStore } from '../../stores/auth.js'

const router = useRouter()
const auth = useAuthStore()

const adminName = (auth.user?.real_name || auth.user?.username || '管理员').slice(0, 8)

const stats = [
  { label: '系统用户',   value: 248, unit: '人', color: 'blue',   icon: 'user', trend: '2 学生 · 1 教师 · 本周新增' },
  { label: '课程总数',   value: 36,  unit: '门', color: 'green',  icon: 'book', trend: '已发布 28 门' },
  { label: '实验模块',   value: 14,  unit: '个', color: 'orange', icon: 'experiment', trend: '含 Jupyter / 沙箱环境' },
  { label: '系统健康',   value: 100, unit: '%',  color: 'purple', icon: 'check', trend: '所有服务正常 · 0 告警' },
]

const ledger = [
  { date: '今日', time: '15:42', title: '新建用户账号 3 个',           meta: '2 学生 · 1 教师',           status: 'info',    icon: 'user' },
  { date: '今日', time: '09:30', title: '《机器学习导论》课程已归档',   meta: 'CRS-014 · 已转归档状态',   status: 'neutral', icon: 'clock' },
  { date: '昨日', time: '17:50', title: 'Jupyter 镜像更新至 v1.4',     meta: 'EXP-IMG · 含 PyTorch 2.3',  status: 'success', icon: 'settings' },
  { date: '11.12', time: '11:08', title: '系统例行巡检完成',           meta: '所有服务正常 · 0 告警',     status: 'success', icon: 'check' },
]

const sections = [
  { num: '01', label: '用户管理',   sub: 'Users',        desc: '创建、编辑、管理用户账号与角色权限',  path: '/admin/users',        icon: 'user', color: 'blue' },
  { num: '02', label: '教务管理',   sub: 'Academics',    desc: '维护学期、教学班与学生名单',          path: '/admin/academics',    icon: 'course', color: 'purple' },
  { num: '03', label: '课程维护',   sub: 'Courses',      desc: '审视与维护全部课程资源',              path: '/admin/courses',      icon: 'book', color: 'green' },
  { num: '04', label: '实验配置',   sub: 'Experiments',  desc: '配置与维护实验模块、镜像与数据集',    path: '/admin/experiments',  icon: 'experiment', color: 'orange' },
  { num: '05', label: '环境档位',   sub: 'Environments', desc: '维护受控包目录与不可变判题/实验环境',  path: '/admin/environments', icon: 'settings', color: 'purple' },
  { num: '06', label: '练习题库',   sub: 'Practice',     desc: '维护全平台练习题、判题方式与 AI 额度', path: '/admin/practice/problems', icon: 'code', color: 'green' },
]

function go(p) { router.push(p) }
function statusText(s) {
  return s === 'success' ? '完成' : s === 'warning' ? '待处理' : s === 'neutral' ? '归档' : '通知'
}
</script>

<template>
  <AppLayout content-mode="list">
    <div class="dash">
      <!-- ── Hero / Page Head ──────────────────────────────────────────── -->
      <header class="page-head hero">
        <div class="hero-content">
          <div class="hero-eyebrow">
            <span class="eyebrow-dot"></span>
            <span>系统管理 · 运行中</span>
          </div>
          <h1 class="hero-title">
            管理控制台，{{ adminName }}
          </h1>
          <p class="hero-sub">
            全部服务运行正常，最近 24 小时 <strong>0 告警</strong>，已处理 <strong>4 项</strong>变更。
          </p>
          <div class="hero-actions">
            <button class="btn-primary" @click="go('/admin/users')">
              管理用户
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                <path d="M3 8h10 M9 4l4 4-4 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
            <button class="btn-ghost" @click="go('/admin/experiments')">
              实验配置
            </button>
          </div>
        </div>
        <div class="hero-visual" aria-hidden="true">
          <div class="health-card">
            <div class="health-emoji"><AppIcon name="check" :size="22" /></div>
            <div class="health-body">
              <div class="health-num">100<span class="health-unit">%</span></div>
              <div class="health-label">系统健康度</div>
            </div>
          </div>
        </div>
      </header>

      <!-- ── Stats ──────────────────────────────────────────────────────── -->
      <section class="stats">
        <article v-for="s in stats" :key="s.label" class="card stat-card" :class="'stat-' + s.color">
          <div class="stat-icon"><AppIcon :name="s.icon" :size="18" /></div>
          <div class="stat-body">
            <div class="stat-value">
              <span class="stat-num">{{ s.value }}</span>
              <span class="stat-unit">{{ s.unit }}</span>
            </div>
            <div class="stat-label">{{ s.label }}</div>
            <div class="stat-trend">{{ s.trend }}</div>
          </div>
        </article>
      </section>

      <!-- ── Main grid ──────────────────────────────────────────────────── -->
      <div class="dash-grid">
        <!-- 变更纪要 -->
        <section class="card panel-card">
          <div class="panel-head">
            <div>
              <h2 class="panel-title">变更纪要</h2>
              <p class="panel-sub">近期用户、课程与实验的变动记录</p>
            </div>
            <span class="badge badge-info">Ledger</span>
          </div>
          <ul v-if="ledger.length" class="timeline">
            <li v-for="(a, i) in ledger" :key="i" class="tl-item">
              <div class="tl-icon" :class="'tl-' + a.status"><AppIcon :name="a.icon" :size="15" /></div>
              <div class="tl-body">
                <div class="tl-title">{{ a.title }}</div>
                <div class="tl-meta">
                  <span class="tl-meta-code">{{ a.meta }}</span>
                  <span class="tl-sep">·</span>
                  <span class="tl-time">{{ a.date }} {{ a.time }}</span>
                </div>
              </div>
              <span class="badge" :class="'badge-' + a.status">{{ statusText(a.status) }}</span>
            </li>
          </ul>
          <div v-else class="empty-state">
            <p>暂无变更记录</p>
          </div>
        </section>

        <!-- 管理入口 -->
        <aside class="card panel-card">
          <div class="panel-head">
            <div>
              <h2 class="panel-title">管理入口</h2>
              <p class="panel-sub">快速进入各管理模块</p>
            </div>
          </div>
          <div class="section-list">
            <button
              v-for="s in sections" :key="s.num"
              class="section-row"
              :class="'sec-' + s.color"
              @click="go(s.path)"
            >
              <div class="sec-icon"><AppIcon :name="s.icon" :size="17" /></div>
              <div class="sec-body">
                <div class="sec-label">
                  <span class="sec-zh">{{ s.label }}</span>
                  <span class="sec-num">{{ s.num }}</span>
                </div>
                <p class="sec-desc">{{ s.desc }}</p>
              </div>
              <svg class="sec-arrow" width="14" height="14" viewBox="0 0 16 16" fill="none">
                <path d="M5 3l5 5-5 5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
          </div>
        </aside>
      </div>
    </div>
  </AppLayout>
</template>

<style scoped>
/* ═══════════════════════════════════════════════════════════════════════
   Admin Dashboard — Code Studio
   Hero + KPI stats + activity ledger + section directory.
   设计系统：全局 card / badge / btn-*，颜色全用 var()
   ═══════════════════════════════════════════════════════════════════════ */
.dash { display: flex; flex-direction: column; gap: 32px; }

/* ── Hero ─────────────────────────────────────────────────────────── */
.hero {
  background: linear-gradient(135deg, var(--fg) 0%, var(--accent-hover) 70%, var(--accent) 100%);
  border-radius: var(--radius-lg);
  padding: 36px 36px;
  color: var(--surface);
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 24px;
  align-items: center;
  position: relative;
  overflow: hidden;
  box-shadow: var(--shadow-xl);
}
.hero::before {
  content: '';
  position: absolute; inset: 0;
  background-image:
    radial-gradient(circle at 85% 20%, oklch(0.66 0.14 75 / 0.3) 0%, transparent 45%),
    radial-gradient(circle at 20% 80%, oklch(0.52 0.09 235 / 0.18) 0%, transparent 50%);
  pointer-events: none;
}
.hero::after {
  content: '';
  position: absolute; inset: 0;
  background-image:
    linear-gradient(oklch(0.99 0.001 95 / 0.04) 1px, transparent 1px),
    linear-gradient(90deg, oklch(0.99 0.001 95 / 0.04) 1px, transparent 1px);
  background-size: 28px 28px;
  pointer-events: none;
}

.hero-content { position: relative; z-index: 1; }
.hero-eyebrow {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 5px 11px;
  background: oklch(0.99 0.001 95 / 0.1);
  border: 1px solid oklch(0.99 0.001 95 / 0.15);
  border-radius: var(--radius-full);
  font-size: var(--text-xs);
  color: oklch(0.99 0.001 95 / 0.85);
  font-weight: 500;
  margin-bottom: 14px;
}
.eyebrow-dot {
  width: 6px; height: 6px;
  background: var(--success);
  border-radius: 50%;
  box-shadow: 0 0 0 3px oklch(0.55 0.13 150 / 0.3);
  animation: pulse 2s infinite;
}
@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 3px oklch(0.55 0.13 150 / 0.3); }
  50%      { box-shadow: 0 0 0 6px oklch(0.55 0.13 150 / 0); }
}
.hero-title {
  font-size: 30px;
  font-weight: 700;
  color: var(--surface);
  letter-spacing: -0.02em;
  line-height: 1.15;
  margin: 0 0 10px;
}
.hero-sub {
  font-size: 15px;
  color: oklch(0.99 0.001 95 / 0.8);
  line-height: 1.55;
  margin: 0 0 18px;
}
.hero-sub strong { color: var(--warning-bg); font-weight: 600; }
.hero-actions {
  display: flex; gap: 10px; flex-wrap: wrap;
}
.hero-actions .btn-primary {
  background: var(--surface);
  color: var(--accent-hover);
  border-color: var(--surface);
  font-weight: 600;
}
.hero-actions .btn-primary:hover {
  background: oklch(0.99 0.001 95 / 0.92);
  color: var(--accent-hover);
  box-shadow: var(--shadow-md);
}
.hero-actions .btn-ghost {
  background: oklch(0.99 0.001 95 / 0.08);
  color: var(--surface);
  border: 1px solid oklch(0.99 0.001 95 / 0.2);
}
.hero-actions .btn-ghost:hover {
  background: oklch(0.99 0.001 95 / 0.15);
  color: var(--surface);
  border-color: oklch(0.99 0.001 95 / 0.3);
}

.hero-visual { position: relative; z-index: 1; }
.health-card {
  display: flex; align-items: center; gap: 14px;
  background: oklch(0.99 0.001 95 / 0.08);
  border: 1px solid oklch(0.99 0.001 95 / 0.15);
  border-radius: var(--radius-lg);
  padding: 18px 22px;
  backdrop-filter: blur(10px);
}
.health-emoji { font-size: 32px; line-height: 1; }
.health-num {
  font-size: 28px;
  font-weight: 700;
  color: var(--warning-bg);
  letter-spacing: -0.02em;
  line-height: 1;
}
.health-unit {
  font-size: 13px;
  color: oklch(0.99 0.001 95 / 0.7);
  font-weight: 500; margin-left: 4px;
}
.health-label {
  font-size: 11px;
  color: oklch(0.99 0.001 95 / 0.7);
  font-weight: 500; letter-spacing: 0.04em; margin-top: 4px;
}

/* ── Stats — 复用 .card ────────────────────────────────────────── */
.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}
.stat-card {
  padding: 18px;
  display: flex; align-items: flex-start; gap: 14px;
  transition: border-color var(--duration-normal) var(--ease-out),
              box-shadow var(--duration-normal) var(--ease-out),
              transform var(--duration-fast) var(--ease-out);
}
.stat-card:hover { transform: translateY(-2px); }
.stat-icon {
  width: 40px; height: 40px;
  border-radius: var(--radius-md);
  display: flex; align-items: center; justify-content: center;
  font-size: 18px; flex-shrink: 0;
  background: var(--accent-soft);
}
.stat-blue .stat-icon   { background: var(--accent-soft); }
.stat-orange .stat-icon { background: var(--accent-soft); }
.stat-purple .stat-icon { background: var(--info-bg); }
.stat-green .stat-icon  { background: var(--success-bg); }

.stat-body { flex: 1; min-width: 0; }
.stat-value {
  display: flex; align-items: baseline; gap: 3px;
  font-family: var(--font-display);
  font-weight: 700; margin-bottom: 2px;
}
.stat-num {
  font-size: 24px;
  color: var(--fg);
  letter-spacing: -0.02em; line-height: 1;
}
.stat-unit {
  font-size: var(--text-xs);
  color: var(--muted); font-weight: 500;
}
.stat-label {
  font-size: var(--text-sm);
  color: var(--fg); font-weight: 500; margin-bottom: 4px;
}
.stat-trend {
  font-size: 11px;
  color: var(--faint);
  font-family: var(--font-mono);
}

/* ── Dash grid ─────────────────────────────────────────────────────── */
.dash-grid {
  display: grid;
  grid-template-columns: 1.4fr 1fr;
  gap: 24px;
  align-items: start;
}

/* 面板卡片 */
.panel-card { padding: 24px; }
.panel-head {
  display: flex; justify-content: space-between; align-items: flex-start;
  margin-bottom: 18px;
}
.panel-title {
  font-size: 17px; font-weight: 600;
  color: var(--fg); letter-spacing: -0.01em; margin: 0;
}
.panel-sub {
  font-size: var(--text-xs); color: var(--muted); margin: 3px 0 0;
}

/* 时间线 */
.timeline { list-style: none; padding: 0; margin: 0; }
.tl-item {
  display: flex; align-items: flex-start; gap: 12px;
  padding: 14px 0;
  border-bottom: 1px solid var(--border);
}
.tl-item:first-child { padding-top: 0; }
.tl-item:last-child { border-bottom: none; padding-bottom: 0; }
.tl-icon {
  width: 36px; height: 36px;
  border-radius: var(--radius-md);
  display: flex; align-items: center; justify-content: center;
  font-size: 16px; flex-shrink: 0;
  background: var(--accent-soft);
}
.tl-warning .tl-icon { background: var(--accent-soft); }
.tl-info .tl-icon    { background: var(--accent-soft); }
.tl-success .tl-icon { background: var(--success-bg); }
.tl-neutral .tl-icon { background: var(--surface-sunken); }

.tl-body { flex: 1; min-width: 0; }
.tl-title {
  font-size: var(--text-sm); font-weight: 500;
  color: var(--fg); line-height: 1.4; margin-bottom: 4px;
}
.tl-meta {
  display: flex; gap: 6px;
  font-size: 11px; color: var(--muted); flex-wrap: wrap;
}
.tl-meta-code { font-family: var(--font-mono); color: var(--faint); }
.tl-sep { color: var(--faint); }
.tl-time { color: var(--faint); }

/* 管理入口列表 */
.section-list {
  display: flex; flex-direction: column; gap: 10px;
}
.section-row {
  display: flex; align-items: center; gap: 14px;
  background: var(--surface-sunken);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  cursor: pointer; text-align: left; width: 100%;
  transition: border-color var(--duration-fast) var(--ease-out),
              background var(--duration-fast) var(--ease-out),
              transform var(--duration-fast) var(--ease-out),
              box-shadow var(--duration-fast) var(--ease-out);
}
.section-row:hover {
  border-color: var(--border-strong);
  background: var(--surface);
  box-shadow: var(--shadow-md);
  transform: translateY(-1px);
}
.sec-icon {
  width: 38px; height: 38px;
  border-radius: var(--radius-md);
  display: flex; align-items: center; justify-content: center;
  font-size: 17px; flex-shrink: 0;
  background: var(--accent-soft);
}
.sec-blue .sec-icon   { background: var(--accent-soft); }
.sec-orange .sec-icon { background: var(--accent-soft); }
.sec-purple .sec-icon { background: var(--info-bg); }
.sec-green .sec-icon  { background: var(--success-bg); }

.sec-body { flex: 1; min-width: 0; }
.sec-label {
  display: flex; align-items: baseline; gap: 8px;
  margin-bottom: 2px;
}
.sec-zh {
  font-size: var(--text-sm); font-weight: 600;
  color: var(--fg); letter-spacing: -0.005em; line-height: 1.2;
}
.sec-num {
  font-size: 11px;
  font-family: var(--font-mono); color: var(--faint);
  font-weight: 500; letter-spacing: 0.04em;
}
.sec-desc {
  font-size: var(--text-xs); color: var(--muted);
  line-height: 1.4; margin: 0;
}
.sec-arrow {
  color: var(--faint); flex-shrink: 0;
  transition: transform var(--duration-fast) var(--ease-out),
              color var(--duration-fast) var(--ease-out);
}
.section-row:hover .sec-arrow { color: var(--accent); transform: translateX(2px); }

/* ── Responsive ─────────────────────────────────────────────────────── */
@media (max-width: 1024px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  .dash-grid { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
  .hero { grid-template-columns: 1fr; padding: 24px; }
  .hero-visual { display: none; }
  .hero-title { font-size: 24px; }
  .stats { grid-template-columns: 1fr 1fr; gap: 12px; }
  .stat-card { padding: 14px; }
  .stat-num { font-size: 20px; }
  .panel-card { padding: 18px; }
}

.hero { padding: 20px 24px; min-height: 0; }
.hero-title { font-size: 32px; }
.hero-visual { max-height: 120px; }
.stat-card { min-height: 72px; padding: 16px; }
.dash-grid { grid-template-columns: minmax(0, 1fr) 320px; align-items: start; }
</style>
