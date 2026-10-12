<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { heroContent, codeLines, codeOutput } from '../../views/welcome/welcomeContent.js'

defineProps({
  isVisible: { type: Boolean, default: false },
})

defineEmits(['explore', 'login'])

const revealedLines = ref(0)
const showOutput = ref(false)
const showScore = ref(false)

let loopTimer = null
const CYCLE_MS = 7800

function resetDemo() {
  revealedLines.value = 0
  showOutput.value = false
  showScore.value = false
}

function startTyping() {
  resetDemo()
  const interval = setInterval(() => {
    if (revealedLines.value < codeLines.length) {
      revealedLines.value++
    } else {
      clearInterval(interval)
      setTimeout(() => { showOutput.value = true }, 200)
      setTimeout(() => { showScore.value = true }, 500)
    }
  }, 70)
}


onUnmounted(() => {
  if (loopTimer) {
    clearInterval(loopTimer)
    loopTimer = null
  }
})

onMounted(() => {
  startTyping()
  loopTimer = setInterval(() => {
    startTyping()
  }, CYCLE_MS)
})
</script>

<template>
  <section class="hero" :class="{ 'hero--visible': isVisible }" aria-label="平台介绍">
    <div class="hero-bg" aria-hidden="true"></div>

    <div class="hero-inner">
      <div class="hero-left">
        <p class="hero-eyebrow">{{ heroContent.eyebrow }}</p>
        <h1 class="hero-title">{{ heroContent.title }}</h1>
        <p class="hero-desc">{{ heroContent.description }}</p>
        <div class="hero-actions">
          <button class="hero-btn hero-btn--primary" @click="$emit('explore')">
            {{ heroContent.primaryAction }}
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="M8 3v10M3 8l5-5 5 5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </button>
          <button class="hero-btn hero-btn--secondary" @click="$emit('login')">
            {{ heroContent.secondaryAction }}
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="M3 8h10M9 4l4 4-4 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </button>
        </div>
      </div>

      <div class="hero-right">
        <div class="code-window">
          <div class="code-header">
            <span class="code-dots" aria-hidden="true">
              <i></i><i></i><i></i>
            </span>
            <span class="code-file">experiment.py</span>
            <span class="code-status">RUNNING</span>
          </div>
          <div class="code-body">
            <div class="code-lines" aria-hidden="true">
              <span v-for="n in codeLines.length" :key="n">{{ n }}</span>
            </div>
            <pre class="code-content"><code v-for="(line, i) in codeLines" :key="i" :class="{ visible: i < revealedLines }" :style="{ paddingLeft: line.indent * 20 + 'px' }">{{ line.text }}</code></pre>
          </div>

          <!-- Output strip: now inside the dark code window -->
          <div class="code-footer" :class="{ visible: showOutput }">
            <div class="code-footer-meta">
              <span class="code-footer-item">
                <span class="code-footer-label">Epochs</span>
                <strong>{{ codeOutput.epochs }}</strong>
              </span>
              <span class="code-footer-item">
                <span class="code-footer-label">Accuracy</span>
                <strong class="txt-accent">{{ codeOutput.accuracy }}</strong>
              </span>
              <span class="code-footer-item">
                <span class="code-footer-label">Tests</span>
                <strong class="txt-success">{{ codeOutput.testsPassed ? '全部测试通过' : '' }}</strong>
              </span>
            </div>
          </div>
        </div>

        <!-- Score panel: light surface for clear contrast with the code window -->
        <div class="score-strip" :class="{ visible: showScore }">
          <div class="score-badge">AI</div>
          <div class="score-text">
            <span class="score-label">{{ codeOutput.scoreLabel }}：优秀</span>
            <span class="score-comment">{{ codeOutput.scoreComment }}</span>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.hero {
  position: relative;
  overflow: hidden;
  padding: 80px 56px 60px;
  background: var(--surface-subtle);
}

.hero-bg {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse 80% 60% at 80% 20%, oklch(0.52 0.095 158 / 0.04) 0%, transparent 60%),
    radial-gradient(ellipse 50% 40% at 20% 80%, oklch(0.52 0.09 235 / 0.03) 0%, transparent 60%);
  pointer-events: none;
}

.hero-inner {
  position: relative;
  max-width: 1180px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 64px;
  align-items: center;
}

.hero-left {
  max-width: 520px;
}

.hero-eyebrow {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.18em;
  color: var(--info);
  margin: 0 0 16px;
}

.hero-title {
  font-size: clamp(34px, 4.5vw, 52px);
  font-weight: 700;
  line-height: 1.1;
  letter-spacing: -0.03em;
  color: var(--fg);
  margin: 0 0 20px;
}

.hero-desc {
  font-size: 16px;
  line-height: 1.65;
  color: var(--muted);
  margin: 0 0 32px;
}

.hero-actions {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.hero-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 14px 28px;
  font-size: 15px;
  font-weight: 600;
  font-family: inherit;
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all 0.2s ease;
}

.hero-btn--primary {
  background: var(--accent);
  color: var(--surface);
}

.hero-btn--primary:hover {
  background: var(--accent-hover);
  transform: translateY(-1px);
  box-shadow: var(--shadow-md);
}

.hero-btn--secondary {
  background: transparent;
  color: var(--fg);
  border: 1px solid var(--border);
}

.hero-btn--secondary:hover {
  border-color: var(--accent);
  color: var(--accent);
}

/* ====== Code Window ====== */
.code-window {
  background: var(--fg);
  border-radius: var(--radius-lg);
  overflow: hidden;
  box-shadow: var(--shadow-lg);
}

.code-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid oklch(0.99 0.001 95 / 0.06);
}

.code-dots {
  display: flex;
  gap: 6px;
}

.code-dots i {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}

.code-dots i:nth-child(1) { background: var(--danger); }
.code-dots i:nth-child(2) { background: var(--warning); }
.code-dots i:nth-child(3) { background: var(--success); }

.code-file {
  font-family: var(--font-mono);
  font-size: 12px;
  color: oklch(0.99 0.001 95 / 0.4);
}

.code-status {
  margin-left: auto;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--success);
  background: oklch(0.55 0.13 150 / 0.12);
  padding: 2px 10px;
  border-radius: var(--radius-lg);
}

.code-body {
  display: flex;
  padding: 18px 0;
  min-height: 200px;
}

.code-lines {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  width: 42px;
  padding-right: 14px;
  border-right: 1px solid oklch(0.99 0.001 95 / 0.06);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.7;
  color: oklch(0.99 0.001 95 / 0.2);
  user-select: none;
  flex-shrink: 0;
}

.code-content {
  flex: 1;
  padding: 0 18px;
  margin: 0;
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.7;
  color: var(--border-strong);
  overflow-x: auto;
}

.code-content code {
  display: block;
  opacity: 0;
  transform: translateX(8px);
  transition: all 0.3s ease;
  background: transparent;
  color: var(--border-strong);
  padding: 0;
  border: none;
  border-radius: 0;
}

.code-content code.visible {
  opacity: 1;
  transform: translateX(0);
}

/* ====== Code Footer (output strip inside dark window) ====== */
.code-footer {
  border-top: 1px solid oklch(0.99 0.001 95 / 0.06);
  padding: 14px 20px;
  background: oklch(0 0 0 / 0.18);
  opacity: 0;
  transform: translateY(8px);
  transition: all 0.4s ease;
}

.code-footer.visible {
  opacity: 1;
  transform: translateY(0);
}

.code-footer-meta {
  display: flex;
  gap: 32px;
}

.code-footer-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.code-footer-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: oklch(0.99 0.001 95 / 0.35);
}

.code-footer-item strong {
  font-size: 17px;
  font-weight: 700;
  color: var(--border);
}

.txt-accent { color: var(--info) !important; }
.txt-success { color: var(--success) !important; }

/* ====== Score Strip (light surface beneath the code window) ====== */
.score-strip {
  margin-top: 10px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 14px 18px;
  display: flex;
  gap: 12px;
  align-items: center;
  opacity: 0;
  transform: translateY(8px);
  transition: all 0.4s ease 0.15s;
  box-shadow: var(--shadow-md);
}

.score-strip.visible {
  opacity: 1;
  transform: translateY(0);
}

.score-badge {
  width: 30px;
  height: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, var(--accent), var(--info));
  color: var(--surface);
  font-size: 11px;
  font-weight: 700;
  border-radius: 50%;
  flex-shrink: 0;
}

.score-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.score-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--fg);
}

.score-comment {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.4;
}

/* ====== Responsive ====== */
@media (max-width: 1024px) {
  .hero-inner {
    grid-template-columns: 1fr;
    gap: 48px;
  }
  .hero-left {
    max-width: 100%;
  }
}

@media (max-width: 768px) {
  .hero {
    padding: 48px 24px 40px;
  }
  .hero-actions {
    flex-direction: column;
  }
  .hero-btn {
    width: 100%;
    justify-content: center;
  }
  .code-body {
    min-height: 160px;
  }
  .code-footer-meta {
    gap: 16px;
    flex-wrap: wrap;
  }
}

@media (prefers-reduced-motion: reduce) {
  .code-content code {
    opacity: 1;
    transform: none;
    transition: none;
  }
  .code-footer,
  .score-strip {
    opacity: 1;
    transform: none;
    transition: none;
  }
}
</style>
