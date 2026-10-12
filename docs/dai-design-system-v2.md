# 人工智能基础实验平台前端设计规范 V2 · 「墨松绿 × 岩灰」

> 文档状态：**正式规范，后续所有 UI 开发必须严格遵守本文档。**（2026-08-21）
> 当前仓库只保留一份可执行的设计系统 CSS 源文件；本规范描述其 token、组件语义和页面组合方式。
>
> | 文件 | 作用 |
> | --- | --- |
> | `frontend/src/styles/dai-ds-v2.css` | V2 唯一设计系统源：token + 全部全局组件样式 |
> | `frontend/src/style.css` | 全局入口、迁移期 token 别名和兼容说明 |
> | `frontend/src/styles/teacher-management.css` | 仍在使用的教师列表页迁移桥接层 |
> | `frontend/src/components/` | 真实 Vue 组件中的页面组合与交互实现 |

如后续需要判断“某个样式该怎么做”：先查本规范，再查 `frontend/src/styles/dai-ds-v2.css`，最后查对应的真实 Vue 页面和组件。不得凭已归档的静态设计稿、旧组件 scoped 样式或个人偏好自行发挥。

---

## 0. 文档约定

1. **唯一色彩语法**：新增 UI 颜色一律使用 `oklch()` 或 `color-mix(in oklch, …)`，禁止在组件里新造 hex、rgb、hsl。已有 V2 源文件里出现的 oklch 值可直接沿用。
2. **Token 优先**：组件优先引用 `var(--xxx)`；只有 V2 CSS 中已存在的固定 oklch 值（浮层遮罩、深色代码区等）允许直接写入组件。
3. **类名语义**：全局样式沿用 `dai-ds-v2.css` 的类名。Vue scoped 样式只做布局细节，不复刻或覆盖全局组件语义；确需覆盖时必须显式加注释说明原因。
4. **源文件优先**：本规范与实现冲突时，以 `frontend/src/styles/dai-ds-v2.css` 及真实 Vue 组件为准；迁移桥接层只允许承载尚未迁移页面的兼容规则。
5. **页面模板优先**：代表页已示范的信息层级、密度与组合方式视为强制模式。

---

## 1. 设计方向与总原则

设计方向以当前 V2 token、App Shell 和 AI 评分组件的实际实现为准：

### 1.1 三个配色方向的选型结论

| 方向 | 结论 | 理由 |
| --- | --- | --- |
| **墨松绿 × 岩灰** | **选定** | 绿色直接对应「判题通过 / 正确 / 已验证」；暖岩灰避免冷白，适合长时间工作；与 V1 蓝 / 橙彻底区分，克制耐看 |
| 朱砂 × 纸墨 | 未采用 | 红 / 橙在评分语境易与「错误」混淆，长期使用存在语义风险 |
| 石墨 × 靛蓝 | 未采用 | 仍属蓝紫系，与 V1 蓝色区分不足，用户明确要求避开蓝紫 |

### 1.2 六项核心决策

1. **色彩**：品牌色从 Cyan / 蓝切换为墨松绿；数据可视化使用独立分类色板；**AI 不再使用紫色**。
2. **字体**：标题用思源宋体建立学术工作台性格；正文用思源黑体；数字与代码等宽。替换 V1 的 Inter + 单一黑体。
3. **密度**：侧栏 232px、表头 56px、表格行 40–42px、页边距 24px；**表格成为一等公民，减少“卡片化一切”**。
4. **控件**：圆角收紧至 3 / 5 / 7px；筛选用矩形 Filter Chip；**每屏主按钮只出现一次**；聚焦为绿色描边。
5. **导航**：从深色侧栏改为浅岩灰侧栏，按「教学 / 评分 / 系统」分组；激活态为绿色左栏 + 浅绿底，可收起为 56px 图标栏。
6. **AI**：**证据来源而非魔法**。确定性测试用实线、AI 判断用虚线、教师决策用绿色实线；一眼区分「事实 / 建议 / 终审」。

### 1.3 设计原则

- 数据优先、内容优先、高信息密度、克制、可长期使用。
- 结构化数据用表格；分组用区块标题 + 发丝线 + 浅底；**仅关键面板**使用 7px 圆角卡片。
- 卡片不做投影；阴影只用于浮层。
- 每屏只保留一个品牌色主按钮，其余用次要 / 幽灵 / 危险按钮。
- 空状态不用 emoji；AI 不做“发光魔法”视觉；状态颜色不用于装饰。

---

## 2. Design Tokens（唯一数值来源）

以下数值全部来自 `frontend/src/styles/dai-ds-v2.css` 的 token 定义。**任何实现不得改值或另设同名变量。**

### 2.1 色彩

#### 品牌色

| Token | 值 | 用途 |
| --- | --- | --- |
| `--accent` | `oklch(0.52 0.095 158)` | 墨松绿：主按钮、激活态、教师终审、判题通过语义 |
| `--accent-hover` | `oklch(0.46 0.095 158)` | 主按钮悬停 |
| `--accent-soft` | `color-mix(in oklch, var(--accent) 13%, transparent)` | 浅绿底：选中态、教师证据、accent 徽标 |
| `--accent-faint` | `color-mix(in oklch, var(--accent) 6%, transparent)` | 更浅绿底：批量条、侧栏激活行 |

#### 中性色（暖岩灰，带一丝绿调）

| Token | 值 | 用途 |
| --- | --- | --- |
| `--bg` | `oklch(0.974 0.004 95)` | 页面背景 |
| `--surface` | `oklch(0.995 0.001 95)` | 卡片 / 表格 / 弹窗表面 |
| `--surface-subtle` | `oklch(0.955 0.006 100)` | 表头、浅底区域 |
| `--surface-sunken` | `oklch(0.978 0.004 95)` | 悬停行、下沉 / 次级工作区 |
| `--surface-overlay` | `oklch(0.997 0.001 95)` | 浮层表面备用 |
| `--fg` | `oklch(0.235 0.018 155)` | 主文字（墨色） |
| `--muted` | `oklch(0.455 0.014 155)` | 次级文字 |
| `--faint` | `oklch(0.62 0.012 155)` | 三级文字 / 图标 |
| `--border` | `oklch(0.90 0.008 110)` | 发丝线边框 |
| `--border-strong` | `oklch(0.79 0.012 110)` | 控件 / 表头边框 |

#### 语义色（浅底一律用 `color-mix` 派生）

| 前景 Token | 值 | 浅底 Token | 派生方式 | 语义 |
| --- | --- | --- | --- | --- |
| `--success` | `oklch(0.55 0.13 150)` | `--success-bg` | mix 13% | 通过 / 已评分 |
| `--warning` | `oklch(0.66 0.14 75)` | `--warning-bg` | mix 14% | 待处理 / 逾期 / 待评分 |
| `--danger` | `oklch(0.54 0.20 25)` | `--danger-bg` | mix 11% | 失败 / 删除 |
| `--info` | `oklch(0.52 0.09 235)` | `--info-bg` | mix 11% | 提示 / AI 判断 |

#### 数据可视化色板（与 UI 强调色分离，品牌色不混入图表）

| Token | 值 | 名称 |
| --- | --- | --- |
| `--viz-1` | `oklch(0.55 0.10 158)` | pine |
| `--viz-2` | `oklch(0.70 0.15 78)` | amber |
| `--viz-3` | `oklch(0.52 0.09 235)` | slate |
| `--viz-4` | `oklch(0.58 0.13 45)` | clay |
| `--viz-5` | `oklch(0.55 0.08 185)` | teal |
| `--viz-6` | `oklch(0.60 0.06 120)` | sage |

#### 仅存在于 V2 CSS 中的固定 oklch 值

- Modal 遮罩：`oklch(0.2 0.01 150 / 0.35)`
- Drawer 遮罩：`oklch(0.2 0.01 150 / 0.28)`
- 代码区主题（`.code-panel` / `.term` 系列，见第 6.18 节）：背景 `oklch(0.225 0.018 155)`、头部 `oklch(0.20 0.016 155)`、边框 `oklch(0.32 0.02 155)` 等。
- 除以上来源外，**组件内不得新造 oklch 数值**；需要深浅变化时用 `color-mix(in oklch, var(--token) N%, transparent/black/white)`。

### 2.2 字体体系

| Token | 值 | 使用 |
| --- | --- | --- |
| `--font-display` | `'Source Serif 4', 'Charter', 'Iowan Old Style', Georgia, 'Noto Serif SC', 'Source Han Serif SC', 'Songti SC', 'SimSun', serif` | h1 / h2 结构标题、wordmark |
| `--font-body` | system + `'PingFang SC', 'Noto Sans SC', 'Source Han Sans SC', 'Microsoft YaHei', sans-serif` | 正文、控件、表格 |
| `--font-mono` | `'JetBrains Mono', 'SF Mono', 'IBM Plex Mono', ui-monospace, 'Cascadia Code', Menlo, Consolas, monospace` | 数字、代码、eyebrow、meta、标签 |

### 2.3 字号（密度优先，正文 14px）

| Token | px | 用途 |
| --- | --- | --- |
| `--text-xs` | 11 | 辅助、代码标注 |
| `--text-sm` | 12 | 表格次级文本、meta |
| `--text-base` | 13 | 表格正文、控件辅助 |
| `--text-md` | 14 | **正文 / 按钮 / 输入** |
| `--text-lg` | 15 | h3 组件标题 |
| `--text-xl` | 17 | 弹窗标题 |
| `--text-2xl` | 20 | h2 区块标题 |
| `--text-3xl` | 24 | h1 页标题 |
| `--text-4xl` | 30 | 概览 hero |

行高：`--lh-tight: 1.3`、`--lh-body: 1.55`、`--lh-relaxed: 1.7`。

### 2.4 间距（4px 网格）

`--space-1: 4`、`--space-2: 8`、`--space-3: 12`、`--space-4: 16`、`--space-5: 20`、`--space-6: 24`、`--space-8: 32`、`--space-10: 40`（单位 px）。

### 2.5 圆角、阴影、布局与控件

| Token | 值 | 规则 |
| --- | --- | --- |
| `--radius-sm` | 3px | badge、小控件、代码 chip |
| `--radius-md` | 5px | 按钮、输入、筛选 chip |
| `--radius-lg` | 7px | 面板、表格外层、弹窗、抽屉 |
| `--radius-full` | 999px | 头像、开关、圆点 |
| `--shadow-sm` | `0 1px 2px oklch(0.2 0.01 150 / 0.05)` | 分段控件激活、开关 |
| `--shadow-md` | `0 6px 18px oklch(0.2 0.01 150 / 0.10)` | 浮层（必要时） |
| `--shadow-lg` | `0 18px 44px oklch(0.2 0.01 150 / 0.16)` | 弹窗 / 抽屉 |
| `--sidebar-width` | 232px | 展开侧栏 |
| `--sidebar-collapsed` | 56px | 收起侧栏 |
| `--header-height` | 56px | 顶栏 |
| `--content-max` | 1440px | 内容最大宽 |
| `--page-pad` | 24px | 页面边距 |
| `--h-btn` | 32px | 按钮标准高 |
| `--h-btn-lg` | 36px | 大按钮 |
| `--h-input` | 32px | 输入标准高 |
| `--h-input-lg` | 36px | 大输入 |

**注意**：`--shadow-md` 目前仅作为 token 存在，样板页面未用于卡片；普通卡片 / 表格外框**不使用阴影**。

---

## 3. Reset、基础排版与布局原语

依据 `dai-ds-v2.css` 第 108–162 行。

### 3.1 基础规则

- `* { box-sizing: border-box }`；body 背景 `--bg`、正文 `--fg`、14px、行高 1.55、antialiased。
- 链接继承文字颜色，不下划线、默认不蓝色。
- `:focus-visible`：2px `--accent` 实线 + 2px offset。
- `::selection`：`--accent-soft`。
- `.sr-only` 用于辅助技术文本。

### 3.2 排版类

| 类 | 规则 |
| --- | --- |
| `h1` / `h2` | 衬线、600、tight、-0.01em；24 / 20px |
| `h3` | 15px / 600 无衬线 |
| `h4` | 14px / 600 无衬线 |
| `.lead` | 14px、`--muted`、max 70ch |
| `.eyebrow` | mono 11px、0.08em、uppercase、`--faint` |
| `.meta` | mono 12px、`--muted` |
| `.num` | mono、tabular-nums |
| `.muted` / `.faint` / `.mono` / `.nowrap` | 语义工具类 |

标题默认 margin 为 0；页面结构标题不得再自定义超大字号。

### 3.3 布局原语

`.container`、`.stack`（子项间距 16px）、`.row`（8/12px gap）、`.row-between`、`.row-wrap`（8px gap、可换行）、`.grid-2`、`.grid-3`、`.grid-4`、`.grid-2-1`、`.grid-1-2`、`.grow`。

代表页网格用法：
- 教师工作台双列：`.grid-2-1`（2fr / 1fr）。
- 弹窗内两列字段：`.grid-2`。
- AI 评分详情双栏不用通用 grid，使用页面级 `.workbench`（见 7.4）。

---

## 4. App Shell（应用外壳）

依据 `dai-ds-v2.css` 第 163–257 行，及 4 个代表页的实际拼装。

### 4.1 结构

```html
<div class="shell">            <!-- 可加 .is-collapsed -->
  <aside class="sidebar">…</aside>
  <div class="main">
    <header class="header">…</header>
    <div class="content"><div class="content-inner">…</div></div>
    <!-- 页面级 sticky 底栏（仅评分详情等需要）放在 .content 之后、.main 内 -->
  </div>
</div>
```

- `.shell`：grid，列 `232px 1fr`；收起为 `56px 1fr`。
- `.main`：flex column、`min-width: 0`。
- `.content`：padding `24px`；`.content-inner` 居中 max 1440px。

### 4.2 侧栏 `.sidebar`

- 背景 `--bg`（不是纯白），右侧 `1px solid var(--border)`；sticky 顶栏高度 100vh。
- `.sidebar-head`：56px 高，左右 padding 16px，下边框；内容为 `.wordmark`：
  - `人工智能` 的 `.mark` 用 `--accent`，其余文字 `--fg`；衬线 18px / 700。
  - `small`：10px、`--muted`，副标题（如“基础实验平台”）。
- `.sidebar-nav`：`padding: 12px 8px`，纵向滚动。
- `.nav-group`：分组间距 16px；`.nav-label`：mono 10px、0.09em、uppercase、`--faint`、padding `8px 12px`。
- `.nav-item`：36px 高、`--radius-md`、margin-top 2px、gap 12px、文字 14px / 500 / `--muted`。
  - hover：`--surface-sunken` 底 + `--fg` 文字。
  - active：`--accent-faint` 底 + `--fg` 600 文字；图标 `--accent`；**左侧 2px `--accent` 竖条**（top/bottom 8px）。
  - `.nav-badge`：mono 10px，`--surface-sunken` 底 + `--border` 框 + `--muted` 文字，3px 圆角。
- `.sidebar-foot`：上边框，含 `.user-card`：`.avatar` 28px 圆形 `--accent-soft` / `--accent`；`.u-name` 14px 600；`.u-role` 11px `--muted`。
- 收起态 `.shell.is-collapsed`：只保留图标与首字母，`wordmark small`、`nav-label`、`nav-text`、`nav-badge`、用户信息隐藏；nav-item 居中。
- 教师端分组顺序固定为：**教学 → 评分 → 系统**。其他角色沿用现有路由数据，但分组标题、行高、激活态必须使用同一视觉语言。

### 4.3 顶栏 `.header`

- 56px 高、sticky、`--surface` 底、下边框。
- 左右结构：移动端菜单按钮（`.menu-btn`，桌面隐藏）→ 侧栏收起按钮（可选）→ **面包屑** → 弹性空间 → 全局搜索 → 通知 → 账户。
- 面包屑 `.crumb`：mono 12px、`--muted`；分隔符 `.sep`；当前项 `.current` 为 `--fg`、单行省略。
- `.header-search`：32px 高、320px 宽（最大 40vw）、`--surface-sunken` 底 + `--border`；左侧搜索图标 15px；输入无边框；右侧 `kbd` 快捷键 10px mono。

### 4.4 页面头 `.page-head`

- 左 `.ph-title`：`.eyebrow`（accent 色）→ `h1`（衬线 24px）→ `.lead`（margin-top 6px）。
- 右 `.ph-actions`：次要按钮在前，**唯一主按钮**（常用 `btn-primary btn-lg`）在后。
- 面包屑与页头分离：面包屑在顶栏，页头 eyebrow 才放上下文路径（如 `教学 / 课程`、`提交 #S12894 · 提交时间 …`）。
- 页头 margin-bottom 20px；可 flex-wrap。

---

## 5. 全局组件规范（01–22 组件样板）

以下组件结构、类名与状态以 `frontend/src/styles/dai-ds-v2.css` 和现有 Vue 组件为准。

### 5.1 色彩系统

- 页面只使用第 2.1 节 token；品牌强调只有 `--accent`。
- 语义色含义固定：success = 通过 / 已评分 / 已发布；warning = 待处理 / 待评分 / 逾期；danger = 失败 / 删除；info = 提示 / AI 判断；accent = 教师确认。
- 图表只能使用 `--viz-1 … --viz-6`。

### 5.2 字体系统

- 页标题 `.h1`、区块标题 `.h2` 必须衬线。
- 正文 / 表格正文 / 控件 14px；表格内容 13px；辅助文本 12px / 11px。
- 所有数字（得分、统计、页码、编号）使用 `.num` 或 mono + tabular-nums。
- 中英文混排不单独造字号；使用 token。

### 5.3 按钮

类：`.btn` + `btn-primary` / `btn-secondary` / `btn-ghost` / `btn-danger` / `btn-danger-solid`；尺寸 `btn-lg` / `btn-sm` / `btn-icon`。

| 状态 | 规则 |
| --- | --- |
| 默认 | 32px 高、padding 0 14px、5px 圆角、14px / 500 |
| primary | `--accent` 底、`--surface` 文字、600 字重 |
| primary hover | `--accent-hover`（加深绿色，**不变灰**） |
| secondary | `--surface` 底、`--border-strong` 框、hover 框变 `--fg` |
| ghost | 透明；hover `--surface-sunken` |
| danger | 透明底 + `--danger` 文字 + `--border` 框；hover `--danger-bg` + danger 框 |
| danger-solid | `--danger` 底；hover `color-mix(in oklch, var(--danger) 88%, black)` |
| disabled | opacity .45、not-allowed |
| active | translateY(1px) |
| icon | 宽度 = 高度；SVG 15px |

铁律：
- **每屏只保留一个 `btn-primary`**；页头主操作、弹窗确认、抽屉确认共用一个主按钮层级。
- 删除主操作用 `btn-danger`，必要时 `btn-danger-solid`，不混用 primary。

### 5.4 输入框

结构：

```html
<div class="field">
  <label for="x">字段名</label>
  <input class="input" id="x" />
  <span class="field-hint">提示</span>      <!-- 可选 -->
  <span class="field-error">错误信息</span>  <!-- 可选 -->
</div>
```

- `.field` 纵向 gap 6px；label 12px / 500 / `--muted`；`.field-hint` 11px `--faint`；`.field-error` 11px `--danger`。
- `.input` / `.textarea`：32px 高（textarea 最小 88px、可竖向 resize）、5px 圆角、`--surface` 底、`--border-strong` 框、14px 文字。
- hover：边框 `--fg`；focus：边框 `--accent` + `0 0 0 3px var(--accent-soft)`；invalid：`--danger` 边框 / `--danger-bg` 光环；disabled：`--surface-subtle` + `--faint`。
- `.input-lg` 36px。

### 5.5 下拉选择

```vue
<DsSelect v-model="status" aria-label="状态筛选">
  <option value="all">全部状态</option>
  <option value="active">启用</option>
</DsSelect>
```

- 统一使用全局 `DsSelect`，不要直接新增原生 `select`。组件保留原生表单值、必填、禁用和 `v-model.number` 语义，但由页面内浮层呈现选项，避免系统菜单向上覆盖标题或越出视口。
- 矩形边框控件，非 Pill；默认 32px 高、5px 圆角。菜单优先向下展开；空间不足时才向上，并钳制在 12px 视口安全边距内。
- 支持方向键、Home/End、输入前缀定位、Enter/Space 选择、Escape/Tab 关闭，以及选择后焦点恢复。
- hover 边框 `--fg`；展开/聚焦边框 `--accent` + 3px `--accent-soft` 光环。工具栏宽度通常 130–170px；多选继续使用专用的「复选 + 计数」组件。

### 5.6 搜索

```html
<label class="searchbox">          <!-- 有值时加 .has-value -->
  <svg>…搜索图标…</svg>
  <input class="input" type="search" />
  <button class="clear" aria-label="清空">…</button>
</label>
```

- 32px 高；图标 15px `--faint` 绝对定位左侧 10px；`.input` 左 padding 32px。
- `.clear` 20px、默认隐藏；`.searchbox.has-value .clear` 才显示；hover 清空按钮 `--surface-sunken`。
- 顶栏全局搜索使用 `.header-search` 结构，含 `<kbd>⌘K</kbd>`；列表页搜索复用 `.searchbox`。

### 5.7 Tabs

```html
<div class="tabs" role="tablist">
  <button class="tab active" role="tab" aria-selected="true">全部提交 <span class="count">128</span></button>
</div>
```

- 文字 + 2px 墨松绿下划线；激活态 = 600 字重 + `--accent` underline（left/right 14px、bottom -1px）。
- 非整排胶囊；Tab 横向滚动不换行。
- `.count`：mono 11px、`--surface-sunken` 底；激活 count 为 `--accent` 文字 + `--accent-soft` 底。
- Vue 实现需同步 `role=tablist/tab` 与 `aria-selected`。

### 5.8 筛选

- `.filter-chip`：32px 高、矩形、`--surface` 底 + `--border-strong` 框、13px `--muted`；active 为 `--accent` 框 + `--accent` 文字 + `--accent-faint` 底；hover 边框 / 文字变 `--fg`。
- `.segmented`：`--surface-sunken` 底 + `--border` 框，内部 26px 按钮；active 为 `--surface` 底 + 600 字重 + `--shadow-sm`。
- 分段控件用于视图切换与短期状态（列表 / 网格 / 紧凑），**不与筛选 chip 混用**；避免整排 Pill。

### 5.9 状态徽标

- `.badge`：22px 高、padding 0 8px、**3px 小圆角**（不是胶囊）、12px / 500、前置 `.dot` 6px 圆点。
- 变体：`badge-success`、`badge-warning`、`badge-danger`、`badge-info`、`badge-neutral`、`badge-accent`。
- 语义映射（同时适用于现有 `UiStatusPill` 迁移）：

| 业务状态 | V2 类 |
| --- | --- |
| 已发布 / 已评分 / 运行成功 | `badge-success` |
| 待评分 / 待处理 / 逾期 | `badge-warning` |
| 失败 / 系统错误 | `badge-danger` |
| AI 已评分 / 待复核（AI）/ 进行中 | `badge-info` |
| 草稿 / 已归档 / 待发布 | `badge-neutral` |
| 教师已确认 | `badge-accent` |

- 密集表格可用无底色 `.status-dot`（7px dot + 13px 文字）降噪；不得只靠颜色表达状态，必须有文字。

### 5.10 卡片 / 表面

- `.panel`：`--surface` 底 + `--border` + **7px 圆角**；无阴影。
  - `.panel-head`：padding `12px 16px`、下边框；左侧 `.ph-label` 可放 eyebrow + h3（14px）。
  - `.panel-body`：padding 16px。
- `.panel-flat`：透明、无边框，用于统计区 / 长表单分组。
- `.surface-subtle`：浅底 + 边框 + 5px 圆角，用于代码块、次级工作区。
- **禁止**：每个数据块都包卡片、卡片加 hover 阴影、卡中卡。

### 5.11 表格（一等公民）

结构：

```html
<div class="table-wrap">
  <div class="toolbar">…</div>          <!-- 可选 -->
  <div class="batch-bar">…</div>        <!-- 可选，批量选择时出现 -->
  <div class="table-scroll">
    <table class="ds-table">…</table>
  </div>
  <div class="pagination">…</div>       <!-- 可选 -->
</div>
```

- `.table-wrap`：`--surface` 底 + `--border` + 7px 圆角，overflow hidden。
- `th`：40px 高、`--surface-subtle` 底、12px / 600 / `--muted`、下边框 `--border-strong`、sticky top。
- `td`：42px 高、13px、padding 0 14px、下边框 `--border`、nowrap。
- 行 hover：`--surface-sunken`；选中行 `.selected`：`--accent-faint` 底 + 首列内嵌 2px `--accent` 左边线。
- 单元格类：
  - `.cell-num`：mono、tabular-nums、右对齐。
  - `.cell-main`：500 字重主文字；下方 `.cell-sub` 12px `--muted` 副信息。
  - `.cell-ellipsis`：max 220px 省略。
  - `.col-check`：40px 列；`.col-actions`：60px 右对齐操作列。
  - `.meta`：mono 12px 时间 / 编号。
- 表头排序：`.sortable` / `.sorted` + `.sort-icon`；排序图标 10×14。
- 分页 `.pagination`：12px 16px、上边框、`--surface`；左 `.pg-info` 12px muted；右 `.pg-btns`；`.pg-btn` 30px 高、5px 圆角，active 为 `--accent` 底 + `--surface` 文字。
- 表格用于课程 / 学生 / 提交 / 成绩 / 实验列表；**不用 KPI 卡片替代数据表格**。

### 5.12 弹窗（Modal）

```html
<div class="modal-backdrop">            <!-- fixed inset-0; 点击自身关闭 -->
  <div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head">…h2(17px)+关闭 ghost 图标…</div>
    <div class="modal-body">…</div>
    <div class="modal-foot">取消(ghost) + 主操作(primary)</div>
  </div>
</div>
```

- 宽度 max 560px；7px 圆角；`--shadow-lg`；最高 90vh。
- head/body/foot 用发丝线分隔；footer 主操作在右。
- 交互：`Escape` 关闭、遮罩点击关闭、打开后焦点移入首个输入或操作控件。
- 用于确认、创建等聚焦任务；复杂多段编辑放全屏 / 抽屉，不放 modal。

### 5.13 抽屉（Drawer）

```html
<div class="drawer-backdrop"></div>
<aside class="drawer" role="dialog" aria-modal="true">
  <div class="drawer-head">eyebrow + h2 + 关闭</div>
  <div class="drawer-body">…滚动内容…</div>
  <div class="drawer-foot">取消 + 主操作</div>
</aside>
```

- 右侧 420px、最大 92vw；`--surface` 底、左 `--border`、`--shadow-lg`；body 可滚动。
- 用途：详情辅助、批量筛选、教师复核面板（学生提交页）。
- 交互与 modal 相同（backdrop / Escape）。

### 5.14 导航

见第 4.2 节侧栏规范。激活态为 2px `--accent` 左栏 + `--accent-faint` 底，**不使用大面积色块**。

### 5.15 页头

见第 4.4 节。衬线标题 + 单行说明 + 右侧上下文主操作；保持标题区干净。

### 5.16 工具栏

- `.toolbar`：padding `12px 16px`、下边框、`--surface`、gap 8px、可换行。
- 标准组合（从左到右）：`.searchbox`（260–280px）→ 学期 / 班级 / 状态 `.select` → `.grow` → 清除筛选 ghost → 导出 secondary。
- 提交页增加排序 select 放在最右；视图切换用 `.segmented`。
- 移动端 `.searchbox` 占满整行。

### 5.17 代码区 / Notebook / Terminal

- `.code-panel`：深墨松色（背景 `oklch(0.225 0.018 155)`）+ `--border-strong` + 7px 圆角。
  - `.code-panel-head` 38px、背景 `oklch(0.20 0.016 155)`、下边框 `oklch(0.32 0.02 155)`；`.fname` mono 12px；`.lang` mono 10px；`.cp-actions` 右侧复制 / 展开 / 全屏 `.cp-btn` 28px。
  - `pre`：13px mono、行高 1.65、横向滚动不裁切；文字 `oklch(0.84 0.01 155)`。
  - 行号 `.ln`：44px 宽右对齐、`oklch(0.48 0.015 155)`；证据行 `.ln.hl` 行号变 `--accent`。
  - 轻量语法高亮：`.c-comment`（斜体）、`.c-keyword`（绿）、`.c-string`（黄）、`.c-number`（橙）、`.c-func`（蓝）。
- `.term`：背景 `oklch(0.20 0.016 155)`、36px head + 三个 10px dot；输出文字 `oklch(0.80 0.01 155)`；`.ok` = `--success`，`.fail` = `--danger`。
- 现有 `CodeViewer` / `CodeBlock` / Notebook 代码单元格、AI diff 全部迁到此视觉；若保留 CodeMirror，需用上述 oklch 色板替换 oneDark，高亮行用 `color-mix(in oklch, var(--accent) …)` 派生，不得使用黄色硬编码。

### 5.18 AI 评分工作台

**总原则：AI 是功能，不是视觉主题。不紫、不发光。**

- `.evidence-tag`：mono 11px、22px 高、3px 圆角。
  - `.deterministic`：success 前景 + `--success-bg`。
  - `.ai`：info 前景 + `--info-bg`。
  - `.teacher`：accent 前景 + `--accent-soft`。
- `.evidence-block`：16px padding、7px 圆角。
  - `.deterministic`：实线 `--border` + `--surface`（**事实**）。
  - `.ai`：虚线 `--border-strong` + `--surface-sunken`（**建议**）。
  - `.teacher`：`--accent` 实线 + `--surface`（**终审 / 权威**）。
- `.score-orb`：mono，`.big` 44px / 600 / tabular-nums，`.of` 15px muted；抽屉 / 卡片内可用 32px。
- `.score-bar`：6px 高、3px 圆角、`--surface-sunken` 底；填充默认 `--accent`，`.ok` success，`.warn` warning，`.bad` danger。
- 评分维度行：`row-between` 文字 + `.num` 分数 + `score-bar`。
- 教师最终决策块：eyebrow accent + `badge-accent`（待确认）+ `.score-orb` + 说明 + 调整输入。
- 现有 `StudentAIGradingResult`、`TeacherScoreOverview`、`TeacherReviewPanel` 一律按上述视觉重组，不得保留 V1 蓝色大数字卡和紫 / 蓝 AI 样式。

### 5.19 空状态

```html
<div class="empty">
  <div class="empty-mark">…描线 SVG…</div>
  <h3>标题</h3>
  <p>说明（≤34ch）</p>
  <div class="empty-actions"><button class="btn btn-secondary btn-sm">…</button></div>
</div>
```

- 56px 24px padding、居中；图标为 44px 描线容器 + `--faint`；**不用 emoji**；动作只一个。

### 5.20 加载

- 骨架 `.skeleton`：`--surface-subtle` 底 + 微光动画，保持布局稳定。
- 长任务：`score-bar` 作为进度条 + `.meta` 状态文案（如“判题中 68%”）；现 `UiProgress` 迁到 6px / 3px 圆角 + `--accent` 填充。

### 5.21 错误

- `.error-panel`：`--surface` 底 + 发丝线边框 + **左侧 3px `--danger` 栏**；标题 14px 600 + 说明 13px muted + 右侧重试 secondary 按钮。
- 错误不整屏遮挡，不替换布局。

### 5.22 数据可视化

- 仅用 `--viz-1…6` 独立色板；**品牌绿不混入图表**，避免与成功状态冲突。
- 图例 `.chart-legend` + `.legend-item` + 10×10 `.swatch`（2px 圆角）。
- 条形图 `.bar-chart`：160px 高、柱宽 max 34px、3px 顶部圆角、mono 12px 标签。
- 环形图 `.donut`：120×120，`conic-gradient` 用 viz 色；旁边 `.num` 24px 百分比 + muted 标签。
- 填充编码优先，避免只用描边；网格发丝线。

---

## 6. 代表页模式（必须照此组合）

### 6.1 教师工作台（`/teacher`）

自上而下：
1. `.page-head` 问候区：eyebrow 日期 → h1「你好，张老师」→ lead 提示 → 右 actions（查看公告 secondary + 处理待评分 primary-lg，按钮内计数用 `.num`）。
2. `.metric-strip`（4 列）：普通值 / 普通值 / `.em`（待复核 12，墨松绿）/ `.warn`（近 7 天截止 3，warning）。**指标条是单条边框分隔，不是 4 张 KPI 卡**。
3. `.grid-2-1`：
   - 左 `.panel` 待处理工作：panel-head eyebrow「Work queue」+ h3 + 全部 ghost；body 为 `.work-row` 列表（12px padding、hover `--surface-sunken`），每行 = 紧急点 `.urgency-dot`（high=danger / mid=warning / low=muted，8px 圆点）+ 标题 / meta 两行 + 状态 badge。
   - 右 `.panel` 课程公告：head + body 内 `.meta` 日期 + `.badge-neutral` 课程名 + 14px 500 正文，条目间 `.rule` 发丝线。
4. 「最近提交」区块：`page-head` 简化版（eyebrow + h2 20px + 全部 ghost），随后 `.table-wrap` + `.ds-table`。

`.work-row` / `.urgency-dot` / `.rule` 在参考页内是页面级样式，Vue 实现可做成该视图 scoped 样式或提取为通用列表行组件，但视觉值必须一致。

### 6.2 课程管理（`/teacher/courses`、`/admin/courses`）

1. `.page-head`：eyebrow「教学 / 课程」→ h1 → lead → 导入名单 secondary + 创建课程 primary-lg。
2. `.table-wrap`：
   - `.toolbar`：搜索课程名称 / 编号（260px）→ 学期 select（140px）→ 状态 select（130px）→ grow → 清除筛选 ghost → 导出 secondary-sm。
   - `.batch-bar`（批量选择后显示，默认 hidden）：`--accent-faint` 底 + 下边框，左“已选 n 项”`.num`，右归档 ghost + 删除 danger-sm。
   - `.ds-table`：checkbox 列、课程（`.cell-main` + `.cell-sub`）、学期、班级 / 学生 / 作业 / 实验四列 `.cell-num`、状态 badge、最近更新 `.meta`、行操作 ghost icon。
   - `.pagination`。
3. 创建课程 `.modal`：head eyebrow + h2；body 为 `.field` 单列 + `.grid-2` 两列字段 + textarea；foot 取消 ghost + 创建 primary。必填标记 `*` 用 `--danger` 文字。

### 6.3 提交与评分（`/teacher/submissions`、`/teacher/submissions/unified`）

1. `.page-head`：eyebrow「评分 / 实验提交」→ h1 → lead → 批量导出 secondary + 开始复核 primary-lg。
2. `.metric-strip` 3 列：全部提交 / 待评分 `.em` / 已评分。
3. `.table-wrap`：
   - `.toolbar`：搜索 280px → 课程 select → 实验 / 作业 select → 状态 select → grow → 排序 select（右侧）。
   - `.batch-bar`：批量评分 ghost、导出选中 secondary、删除 danger。
   - 表格：学生主副信息、实验名 `.cell-ellipsis`、课程、状态 badge、测试与 AI 得分 `.cell-num`（AI 得分 600 字重）、提交时间 `.meta`、行操作「复核」ghost-sm。
4. 复核 `.drawer`：head eyebrow 提交号 + 姓名 + h2；body 为 `.evidence-block.ai`（AI 建议 score-orb 32px）+ `.field` 调整得分 + textarea 理由；foot 取消 + 确认并生效 primary。

### 6.4 AI 评分详情（`/teacher/ai-grading/:id`）

1. 顶栏额外放「复制原始 JSON」ghost-sm；面包屑 current 为提交号。
2. `.page-head`：eyebrow 提交号 + 时间 → h1 实验名 → `.row-wrap` 内 `.status-dot`（warning=待教师复核）+ 学生 / 学号 / 运行环境 `.meta`；右侧「查看题目」secondary。
3. `.row-wrap` 证据图例：三个 `.evidence-tag` + muted 文案「实线 = 事实 · 虚线 = 建议 · 绿实线 = 终审」。
4. `.metric-strip` 3 列评分总览：测试结果（label 用 success）、AI 建议分（label 用 info）、教师最终得分（`.em` + label accent）；m-value 26px，配 `.m-sub` 说明。
5. `.workbench`（页面级 grid：`minmax(0, 1.5fr) minmax(0, 1fr)`，≤1024px 单列）：
   - 左列 `.wb-col.stack`：学生代码 `.code-panel`（含行号高亮证据）→ `.evidence-block.deterministic` 测试结果（维度 score-bar + 说明 + `.term` pytest 摘要）。
   - 右列 `.wb-col.stack`：`.evidence-block.ai`（AI 评分依据，虚线，维度 + `.evidence-list` 行号 chip `.ev-chip`）→ `.panel` Rubric 过程分（row-between + score-bar）→ `.evidence-block.teacher`（教师终审，score-orb + 调整得分 + 理由）。
6. `.main` 底部 `.confirm-bar`：sticky bottom、`--surface` 底 + 上边框；左状态说明，右返回列表 ghost + 暂存 secondary + 确认并发布 primary。
7. 发布确认 `.drawer`：发布后得分浅底行 + 必填理由 + 复选确认 + 确认发布 primary。

---

## 7. Vue 3 + Vite 实施规范（旧版 → V2 迁移）

### 7.1 样式挂载方式

- `frontend/src/main.js` 当前导入 `./style.css` 与 `./styles/teacher-management.css`。
- 迁移目标：保留 `style.css` 作为全局入口，但在所有页面完成迁移后删除其中的旧 token 别名；同时删除不再被页面引用的 `teacher-management.css` 桥接规则。`dai-ds-v2.css` 已经是仓库内的唯一 V2 CSS 源，不再从其他目录复制。
- 页面级视觉差异（如 `.batch-bar`、`.workbench`、`.confirm-bar`）优先提取到对应 Vue 组件 scoped style；数值必须使用 token。
- Vue scoped 样式允许存在，但只用于该组件专属布局；全局已定义的 `.btn` / `.input` / `.ds-table` 等类不得在 scoped 中重写为 V1 形态。

### 7.2 禁止与替换清单

| 旧版（废止） | V2 替换 |
| --- | --- |
| `--ink / --paper / --primary / --accent(var(--warning)) / --purple / #…` 等 V1 token | 第 2 节 V2 token |
| 深色侧栏 `--bg-sidebar: #0F172A`、蓝色 logo 块 | 浅岩灰侧栏 + 文字 wordmark |
| 蓝色链接 `a { color: var(--primary) }`、蓝字实体名 | 继承 `--fg`；表格主文字 `.cell-main` |
| `button` 全局 9px 16px、8px 圆角、蓝色主按钮 | `.btn` 32px / 5px 圆角；primary = `--accent` |
| `input` 9px 12px、蓝色聚焦光环 | `.input` 32px、accent 聚焦 |
| 大写表头 + 蓝色悬停 | `.ds-table` sticky 浅灰表头 + `--surface-sunken` hover |
| 全圆角 pill badge | 3px 圆角 `.badge` + dot |
| 卡片 hover 阴影、`--shadow-card` | 卡片无阴影；阴影只给浮层 |
| 紫色 AI / `--purple`、AI emoji 按钮 | evidence 三类视觉；AppIcon `brain` 图标 |
| KPI 卡片 `teacher-metric-card` | `.metric-strip` |
| `body:has(.layout)` 双滚动、fixed 侧栏 margin 方案 | `.shell` grid + sticky sidebar，按 V2 响应式收起 |
| `--radius-card: 12px`、`--radius-control: 7px`、大圆角弹窗 | `--radius-lg: 7px` 卡片 / 弹窗，控件 5px |

### 7.3 现有组件迁移对照

| 旧组件 | V2 目标 |
| --- | --- |
| `AppLayout.vue` | 根 `.shell`；侧栏 `.sidebar`；右侧 `.main`；`.content > .content-inner`；折叠态用 `.is-collapsed`，移动端 `.mobile-nav-open` |
| `AppSidebar.vue` | `.sidebar-head` wordmark；菜单数据保持不变，包进 `.nav-group` + `.nav-label`；`.nav-item` + `.nav-text`；教师组固定「教学 / 评分 / 系统」；底部 `.sidebar-foot` + `.user-card` |
| `AppHeader.vue` | `.header` 56px：菜单按钮（移动端）、面包屑 `.crumb`、`.header-search`、通知 / 账户按钮；用户下拉改用 V2 `.panel` 或浮层 token，退出项 hover `--danger-bg` |
| `AppIcon.vue` | 保留；新图标线宽视觉参照 V2 内联 SVG 的 `stroke-width: 1.7`；禁止 emoji / 自制 SVG / CSS 图案 |
| `UiPanel.vue` | `.panel` + `.panel-head` + `.panel-body`（或 `.panel-flat`） |
| `UiProgress.vue` | `.score-bar` 6px / 3px 圆角；进度色 `--accent` |
| `UiStatusPill.vue` | `.badge` + dot；色调按第 5.9 节映射，删除 `submitted` 紫色（→ `badge-info`） |
| `ConfirmDialog.vue` | `.modal-backdrop` + `.modal` + `.modal-head/body/foot`；z-index 与 V2 浮层一致；不再写 hex / 14px 大圆角 |
| `TeacherPageHeader.vue` | `.page-head`；h1 衬线 24px 600；subtitle → `.lead`；actions 右对齐 |
| `TeacherMetricGrid.vue` | `.metric-strip` + `.metric`；`.em` / `.warn` 控制语义值颜色；删除 icon 大色块与单卡阴影 |
| `TeacherPagination.vue` | `.pagination` + `.pg-info` + `.pg-btn`；跳页输入用 `.input` token |
| `SubmissionReviewPanel.vue` | 按场景使用 `.drawer`（列表页复核）或 `.evidence-block.teacher`（详情页）；输入 `.input/.textarea`，主按钮 `btn-primary` |
| `SubmissionSnapshotCell.vue` | 快照单元格迁到 `.code-panel` / `.term` 视觉；复制按钮 `.cp-btn`；输出错误 `.fail`；删除 `#F8FAFD` 等 hex |
| `CodeViewer.vue` | `.code-panel` 深色头 + 行号 + 证据行 `.ln.hl`；CodeMirror 主题按第 5.17 节 oklch 色板替换 oneDark |
| `TeacherScoreOverview.vue` | 改为评分总览 `.metric-strip`（测试 / AI / 教师三列）或 `.evidence-block` + `.score-orb`；维度条 `.score-bar`；删除 64px 蓝色大数字卡 |
| `StudentAIGradingResult.vue` | `.evidence-block.ai` + `.score-orb` + `.score-bar`；问题卡用 danger token；diff 用 `.code-panel` / `.term`；删除蓝色进度与紫色 |
| `TeacherReviewPanel.vue` | `.evidence-block.teacher`（accent 实线）+ `.field` 表单 + `.score-orb` 总分预览；确认弹窗用 `.modal`；草稿提示用 `.status-dot` / badge |
| `AiConfigForm.vue` / `AIQuestionConfig.vue` | 全部 hex 替换为 token；字段用 `.field/.input/.textarea/.select`；生成按钮用 `btn-secondary`（图标 brain，不用 emoji）；测试组 / Rubric 行用 `.panel` / `.surface-subtle` / `.badge`；警告用 warning token |
| `CourseFormModal.vue` / `CourseCreateModal.vue` | `.modal` 560–760px；字段 `.field` + `.grid-2`；封面 dropzone 虚线 `--border-strong` + 7px 圆角 + `--surface-subtle`；进度 `.score-bar` |
| `TeachingClassMultiSelect.vue` | 触发器改 `.input` 风格（32px / 5px 圆角 / accent 聚焦）；下拉菜单 `.panel` 浮层 + `.check`；tag 用 `.badge-neutral` + 小 close；删除 hex 蓝底 |
| `CourseCoverUploader.vue` | 同封面规范；错误用 `.error-panel` 或 `--danger-bg` 提示；删除 hex |
| `CourseWhitelistManager.vue` / `CourseRosterManager.vue` | 列表 `.ds-table`；搜索 `.searchbox`；操作 `btn-ghost / btn-danger`；状态 badge；空态 `.empty` |
| `App.vue` toast | V2 无内置 toast，按扩展规则实现：`--surface` 底 + `--border` + 左 3px 语义色（success/danger/info/warning，无类型时 `--accent`）+ `--shadow-lg`；全部 token，无 hex |
| `teacher-management.css` 覆盖的列表页 | 数据面板 → `.table-wrap`；filter-bar → `.toolbar`；搜索框 / select → `.searchbox/.select`；蓝色课程链接 → `.cell-main`；status-pill → `.badge`；KPI 卡片 → `.metric-strip` |

### 7.4 未包含在 22 个样板内的组件扩展规则

规范未覆盖 toast、上传、多选下拉、富文本编辑器等业务组件。实现时允许新建样式，但必须：

1. 颜色只使用第 2 节 token 或 V2 源内已存在的 oklch 固定值；
2. 圆角从 `--radius-sm/md/lg` 选择，控件高 28 / 32 / 36px；
3. 浮层阴影用 `--shadow-lg`；
4. 语义反馈使用 `*-bg` 浅底 + 语义前景；
5. 不使用 emoji、渐变发光、紫色 AI 主题。

### 7.5 状态与语义统一

- 提交 / 评分状态词与徽标映射以第 5.9 节表为准。
- 得分数字全部 mono + tabular-nums；主得分数 600–700 字重。
- AI 证据三种视觉在任何页面不得混用：测试 = 实线 success，AI = 虚线 info，教师 = accent 实线。

---

## 8. 响应式契约

以 `frontend/src/styles/dai-ds-v2.css` 的响应式规则为准：

| 断点 | 行为 |
| --- | --- |
| ≤1024px | `.metric-strip` 变 2 列（第 2 项右框去掉，前两项下边框）；`.grid-2/.grid-3/.grid-4/.grid-2-1/.grid-1-2` 全部单列；AI `.workbench` 单列（页面级样式） |
| ≤820px | `.shell` 单列；`.sidebar` 变 fixed 抽屉（`transform: translateX(-100%)`，`.mobile-nav-open` 打开）；`.menu-btn` 显示；`.header-search` 隐藏；内容 padding 16px；表格 cell 40px / padding 10px；`.page-head` 纵向 |
| ≤560px | `.metric-strip` 单列（最后项无下边框）；工具栏搜索全宽；按钮高度 44px，`btn-sm / btn-icon` 40px（移动端触摸目标） |

现有 Vue 中的 1199 / 767.98 断点体系**废止**，改用上述三档；组件内部如需补断点只能比 820 / 560 更细，不得恢复 V1 断点。

---

## 9. 可访问性与交互基线

- 全站 `:focus-visible`：2px `--accent` + 2px offset。
- Tabs：`role=tablist/tab` + `aria-selected`；激活样式与 ARIA 同步。
- Modal / Drawer：`role=dialog` + `aria-modal=true` + `aria-labelledby`；`Escape` 关闭、遮罩点击关闭、关闭后焦点返回；打开后焦点移入首个输入或操作。
- 批量选择：全选 / 行选 checkbox 与 `tr.selected` 同步；`.batch-bar` 使用 `hidden` 并更新“已选 n 项”。
- 状态不得只靠颜色：badge 有文字，status-dot 有文字，图表有图例。
- 空状态 / 加载 / 错误分别使用第 5.19–5.21 组件；错误要提供重试。
- 保留旧版 `prefers-reduced-motion` 降级思路：动画时长统一压缩，V2 骨架 shimmer / hover 过渡不得例外。
- 图标一律使用现有 `AppIcon` 图标库，禁止 emoji、自制 SVG、CSS 图案。

---

## 10. 验收检查清单（每个页面 / PR 必查）

1. `frontend/src/styles/dai-ds-v2.css` 已由 `frontend/src/style.css` 接入；旧桥接规则只存在于仍未迁移的页面。
2. 新增样式中 grep 不到 hex / rgb / hsl；只有 V2 token 与 `oklch()` / `color-mix(in oklch, …)`。
3. 页面只有 **一个** `btn-primary`；主操作层级与参考页一致。
4. h1 / h2 为衬线；正文 14px；表格 13px / 42px 行高；控件 32px。
5. 侧栏浅色 232px、教师端分组「教学 / 评分 / 系统」、active 为 2px accent 左栏 + `--accent-faint`。
6. 顶栏 56px：面包屑 + 全局搜索；移动端按 820px 断点变抽屉。
7. 列表页 = page-head →（可选 metric-strip）→ table-wrap（toolbar / batch-bar / ds-table / pagination）；无“满屏卡片”。
8. 徽标为 3px 圆角 + 圆点；语义映射正确。
9. AI 页面：evidence-tag 与 evidence-block 三种语义正确；无紫色、无发光；教师终审为 accent 实线。
10. 代码区为深墨松绿主题，行号 / 高亮 / 终端 PASSED-FAILED 符合 `.code-panel` / `.term`。
11. 空态无 emoji；错误面板左侧 3px danger 栏；加载用骨架 / score-bar。
12. 图表仅 `--viz-1…6`。
13. 弹窗 / 抽屉结构完整、遮罩与 Escape 交互可用、焦点管理正确。
14. 三档响应式行为达标，且不残留 V1 的 1199 / 767.98 规则。

---

## 附录 A：当前实现映射

| 实现位置 | 责任 |
| --- | --- |
| `frontend/src/styles/dai-ds-v2.css` | token、reset、App Shell、表单、表格、面板、状态、AI 证据、响应式规则 |
| `frontend/src/style.css` | 全局 CSS 入口；迁移期旧 token 别名和少量兼容规则 |
| `frontend/src/styles/teacher-management.css` | 尚未迁移的教师管理列表页桥接；完成迁移后删除 |
| `frontend/src/components/layout/` | Shell、Header、Sidebar 的真实实现 |
| `frontend/src/components/ui/` | AppIcon、面板、进度、状态、确认等通用 UI |
| `frontend/src/components/ai/` | AI 配置、评分证据、代码和教师复核组件 |
| `frontend/src/views/teacher/` | 教师工作台、课程/作业/考试/实验/提交/评分等页面组合 |

## 附录 B：页面级样式清单

| 页面级类 | 来源页面 | 迁移建议 |
| --- | --- | --- |
| `.work-row` / `.urgency-dot` | `frontend/src/views/teacher/DashboardView.vue` | 保持为教师工作台页面级样式 |
| `.rule` | 教师工作台公告区域 | 需要复用时提取为通用列表分隔原语 |
| `.batch-bar` | 教师列表页 | 迁移到通用数据表组件或页面级样式 |
| `.workbench` / `.wb-col` | `AIGradingReviewDetailView.vue` | 保持评分详情双栏布局 |
| `.evidence-list` / `.ev-chip` | AI 评分组件 | 保持事实 / AI 建议 / 教师终审的视觉区分 |
| `.confirm-bar` | AI 评分详情 | 需要 sticky 底栏的工作台页通用组件 |

## 桌面内容布局模式（2026-09 UI 整改）

`AppLayout` 新增可选 `contentMode`，默认 `list`；模板使用 `content-mode`。布局模式独立于原有 `variant`，不改变导航、权限或业务接口。

| 模式 | 使用场景 | 内容宽度 |
| --- | --- | --- |
| `list` | 管理列表、首页、统计 | 使用可用宽度 |
| `form` | 用户资料、安全、普通设置 | 最大 960px |
| `reading` | 讲义正文与目录 | 容器最大 1080px，正文约 780px，目录独立 |
| `workspace` | 代码、题目编辑、评分、Notebook | 使用可用宽度，页面按任务分栏 |

```vue
<AppLayout content-mode="form">
  <section class="account-page">...</section>
</AppLayout>
```

`AppLayout` 已提供主 `main` 地标，页面内容使用 `section` 或 `div`，避免重复地标。桌面页面沿用 24px 边距，普通标题 24px，首页不超过 32px。使用已有间距变量，不重复设置容器左右偏移或负外边距。

列表按标题/主操作、紧凑统计、筛选、数据、分页排列。长班级信息可复用 `ClassSummary`，用原生展开交互保留完整内容。窄选择栏通常 280px，主名单/编辑区使用 `minmax(0, 1fr)`；设置高度限制的 Flex 列表必须避免条目收缩重叠。

弹窗标题与操作区不收缩，正文拥有滚动区。缺少焦点管理的本地弹窗可引入 `vDialogFocus`，它负责初始焦点、Tab 循环及关闭后返回触发控件；Escape、取消和忙碌状态仍由原组件处理。已有独立焦点管理的弹窗不可重复绑定。
