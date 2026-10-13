/** 题目编辑页 QuestionEditView（IDE 风格布局重构）
 *
 * 覆盖：
 * - 布局：作业默认环境卡（draft 可编辑 / published 禁用）、保存作业环境设置
 * - 创建题目：函数签名自动解析函数名；inherit（环境 null）与 override（指定版本）payload 正确
 * - restricted 白名单随 payload 提交；内存低于环境最低值显示警告卡 + 「使用推荐值」按钮
 * - 表格化公开样例：添加/行内编辑/批量导入/分页，保存后序列化为原 JSON 数组格式
 * - 私有测试可视化表格保存为 pytest 代码（hidden_tests 契约不变）
 * - 运行测试调用 sample-run（后端仅学生可用，教师端 403 如实展示）
 * - 编辑已有题目回填并调用 updateQuestion
 * - 学生代码模板为深色 CodeMirror（固定高度，不随代码行数增长）
 */
import { describe, it, expect, vi, beforeEach, beforeAll, afterAll, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const navigation = vi.hoisted(() => ({ route: { params: { id: '7' }, query: {}, meta: { role: 'teacher' } }, push: vi.fn() }))

vi.mock('vue-router', () => ({
  useRoute: () => navigation.route,
  useRouter: () => ({ push: navigation.push }),
  createRouter: vi.fn(() => ({
    beforeEach: vi.fn(),
    afterEach: vi.fn(),
    beforeResolve: vi.fn(),
    push: vi.fn(),
    replace: vi.fn(),
    currentRoute: { value: { path: '/teacher/assignments/7/edit' } },
  })),
  createWebHistory: vi.fn(() => ({})),
}))

vi.mock('../../../api/assignments', () => ({
  assignmentsAPI: {
    get: vi.fn(),
    getQuestions: vi.fn(),
    createQuestion: vi.fn(),
    updateQuestion: vi.fn(),
    deleteQuestion: vi.fn(),
    update: vi.fn(),
    publish: vi.fn(),
  },
}))

vi.mock('../../../api/environments', () => ({
  environmentsAPI: { listAvailable: vi.fn() },
}))

vi.mock('../../../api/judge', () => ({
  judgeAPI: { sampleRun: vi.fn() },
}))

vi.mock('../../../api/aiGrading.js', () => ({
  aiGradingAPI: {
    getStatus: vi.fn().mockResolvedValue({ data: { enabled: true, ready: true } }),
    getConfig: vi.fn().mockResolvedValue({ data: { grading_mode: 'legacy', teacher_constraints: {}, reference_solution: null, test_groups: [], score_cap_rules: [] } }),
    updateConfig: vi.fn().mockResolvedValue({ data: { ok: true } }),
    listRubrics: vi.fn().mockResolvedValue({ data: { items: [] } }),
    generateRubric: vi.fn().mockResolvedValue({ data: { id: 1, version: 1, status: 'draft', rubric_json: {} } }),
    lockRubric: vi.fn().mockResolvedValue({ data: { ok: true } }),
    generateTestGroups: vi.fn(),
  },
}))

const appStore = { showToast: vi.fn() }
vi.mock('../../../stores/app', () => ({
  useAppStore: () => appStore,
}))

import { assignmentsAPI } from '../../../api/assignments.js'
import { environmentsAPI } from '../../../api/environments.js'
import { judgeAPI } from '../../../api/judge.js'
import { aiGradingAPI } from '../../../api/aiGrading.js'

const envOptions = [
  {
    profile_id: 1, environment_version_id: 11, slug: 'basic', display_name: 'Python 基础',
    version_number: 1, packages: [{ pip_name: 'pytest', locked_version: '8.3.4', import_names: ['pytest'] }],
    minimum_memory_mb: 256,
  },
  {
    profile_id: 2, environment_version_id: 22, slug: 'data', display_name: '数据分析',
    version_number: 1, packages: [{ pip_name: 'numpy', locked_version: '2.1.3', import_names: ['numpy'] }],
    minimum_memory_mb: 768,
  },
]

const assignmentDraft = {
  id: 7, title: '环境作业', status: 'draft', course_id: 1,
  environment_version_id: 11, import_policy_mode: 'unrestricted', allowed_imports: [],
  published_at: null, due_at: null,
}

// jsdom 未实现 scrollTo（页面编辑状态切换时调用），静默掉避免噪音
beforeAll(() => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {})
})

// CodeMirror 在 jsdom 中需要 Range 测量 polyfill（与 CodeCell.spec 同款模式）
const originalRangeGetClientRects = Range.prototype.getClientRects
beforeAll(() => {
  if (!originalRangeGetClientRects) {
    Object.defineProperty(Range.prototype, 'getClientRects', {
      configurable: true,
      value: () => [],
    })
  }
})
afterAll(() => {
  if (!originalRangeGetClientRects) {
    delete Range.prototype.getClientRects
  }
})

const wrappers = []
afterEach(() => {
  for (const wrapper of wrappers.splice(0)) wrapper.unmount()
})

async function mountPage() {
  const mod = await import('../QuestionEditView.vue')
  const wrapper = mount(mod.default, {
    global: {
      stubs: {
        AppLayout: { template: '<div><slot /></div>' },
        AIQuestionConfig: {
          name: 'AIQuestionConfig',
          props: ['kind', 'questionId', 'expanded', 'closable'],
          emits: ['saved'],
          template: '<div class="ai-config-stub" />',
        },
      },
    },
  })
  wrappers.push(wrapper)
  return wrapper
}

async function clickBtn(wrapper, text) {
  const btn = wrapper.findAll('button').find((b) => b.text().includes(text))
  expect(btn, `按钮「${text}」应存在`).toBeTruthy()
  await btn.trigger('click')
}

/** 进入新建题目表单（添加题目 → 填标题与签名 → 自动解析函数名） */
async function startNewQuestion(wrapper, title = '新题', signature = 'def add(a: int, b: int) -> int') {
  await clickBtn(wrapper, '添加题目')
  await wrapper.find('input[placeholder="如: 两数之和"]').setValue(title)
  await wrapper.find('input[placeholder^="def add"]').setValue(signature)
  await flushPromises()
}

describe('题目编辑页 QuestionEditView（IDE 布局重构）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    navigation.route = { params: { id: '7' }, query: {}, meta: { role: 'teacher' } }
    appStore.showToast.mockClear()
    assignmentsAPI.get.mockResolvedValue({ data: assignmentDraft })
    assignmentsAPI.getQuestions.mockResolvedValue({ data: { items: [] } })
    environmentsAPI.listAvailable.mockResolvedValue({ data: envOptions })
  })

  it('加载后展示作业默认环境并默认选中', async () => {
    const wrapper = await mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('作业默认环境')
    expect(wrapper.text()).toContain('环境最低内存 256 MB')
  })

  it('管理员从课程资料进入后，按真实任务归属返回资料', async () => {
    navigation.route = { params: { id: '7' }, query: { from: 'materials', course_id: '999', offering_id: '999' }, meta: { role: 'admin' } }
    assignmentsAPI.get.mockResolvedValue({ data: { ...assignmentDraft, course_id: 101, offering_id: 109 } })
    const wrapper = await mountPage(); await flushPromises()
    await clickBtn(wrapper, '返回课程资料')
    expect(navigation.push).toHaveBeenCalledWith({ path: '/admin/materials', query: { course_id: '101', offering_id: '109' } })
  })

  it('管理员刷新编辑地址且加载失败时，按有效查询上下文返回资料', async () => {
    navigation.route = { params: { id: '7' }, query: { from: 'materials', course_id: '101', offering_id: '109' }, meta: { role: 'admin' } }
    assignmentsAPI.get.mockRejectedValueOnce(new Error('unavailable'))
    const wrapper = await mountPage(); await flushPromises()
    await clickBtn(wrapper, '返回课程资料')
    expect(navigation.push).toHaveBeenCalledWith({ path: '/admin/materials', query: { course_id: '101', offering_id: '109' } })
  })

  it('管理员直接进入编辑页返回授课安排，教师仍返回作业列表', async () => {
    navigation.route.meta.role = 'admin'
    const admin = await mountPage(); await flushPromises()
    await clickBtn(admin, '返回授课安排')
    expect(navigation.push).toHaveBeenLastCalledWith('/admin/offerings')
    navigation.route = { params: { id: '7' }, query: {}, meta: { role: 'teacher' } }
    const teacher = await mountPage(); await flushPromises()
    await clickBtn(teacher, '取消')
    expect(navigation.push).toHaveBeenLastCalledWith('/teacher/assignments')
  })

  it('作业环境配置紧跟题目列表，选择题目后仍保持在该位置', async () => {
    assignmentsAPI.getQuestions.mockResolvedValue({
      data: {
        items: [{
          id: 55, assignment_id: 7, title: '旧题', function_name: 'old', hidden_tests: '',
          time_limit_ms: 10000, memory_limit_mb: 512, grading_mode: 'legacy',
        }],
      },
    })
    const wrapper = await mountPage()
    await flushPromises()

    const listCard = wrapper.find('.qe-list-card').element
    const sideElement = wrapper.find('.qe-side').element
    expect(listCard.nextElementSibling).toBe(sideElement)

    await wrapper.find('.qe-list-actions button').trigger('click')
    await flushPromises()

    expect(wrapper.find('.qe-side').element).toBe(sideElement)
    expect(wrapper.find('.qe-list-card').element.nextElementSibling).toBe(sideElement)
    expect(wrapper.find('.qe-side').text()).toContain('作业默认环境')
  })

  it('已发布作业的环境卡禁用且显示提示', async () => {
    assignmentsAPI.get.mockResolvedValue({ data: { ...assignmentDraft, status: 'published' } })
    const wrapper = await mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('环境已锁定')
    // 已发布时作业环境选择器禁用
    expect(wrapper.find('.env-picker-select').attributes('disabled')).toBeDefined()
  })

  it('编辑页展示首次发布时间，延后已发布作业截止时间可直接保存', async () => {
    const published = {
      ...assignmentDraft,
      status: 'published',
      published_at: '2026-08-01T01:00:00Z',
      due_at: '2099-10-01T12:00:00Z',
    }
    assignmentsAPI.get.mockResolvedValue({ data: published })
    assignmentsAPI.update.mockResolvedValue({ data: { ...published, due_at: '2099-11-01T12:00:00Z' } })
    const wrapper = await mountPage()
    await flushPromises()

    expect(wrapper.find('.qe-schedule-card').text()).toContain('首次发布时间')
    await wrapper.find('#assignment-due-at').setValue('2099-11-01T20:00')
    await clickBtn(wrapper, '保存时间设置')
    await flushPromises()

    expect(assignmentsAPI.update).toHaveBeenCalledWith('7', {
      due_at: new Date('2099-11-01T20:00').toISOString(),
    })
    expect(wrapper.find('.confirm-panel').exists()).toBe(false)
  })

  it('编辑页提前截止时间时要求二次确认', async () => {
    const published = {
      ...assignmentDraft,
      status: 'published',
      published_at: '2026-08-01T01:00:00Z',
      due_at: '2099-12-01T12:00:00Z',
    }
    assignmentsAPI.get.mockResolvedValue({ data: published })
    assignmentsAPI.update.mockResolvedValue({ data: published })
    const wrapper = await mountPage()
    await flushPromises()

    await wrapper.find('#assignment-due-at').setValue('2099-11-01T20:00')
    await clickBtn(wrapper, '保存时间设置')
    await flushPromises()
    expect(assignmentsAPI.update).not.toHaveBeenCalled()
    expect(wrapper.find('.confirm-panel').text()).toContain('缩短学生作答时间')

    await clickBtn(wrapper.find('.confirm-panel'), '确认保存')
    await flushPromises()
    expect(assignmentsAPI.update).toHaveBeenCalledWith('7', {
      due_at: new Date('2099-11-01T20:00').toISOString(),
    })
  })

  it('保存作业环境设置调用 update，payload 携带环境与策略', async () => {
    assignmentsAPI.update.mockResolvedValue({ data: assignmentDraft })
    const wrapper = await mountPage()
    await flushPromises()
    await clickBtn(wrapper, '保存作业环境设置')
    await flushPromises()
    expect(assignmentsAPI.update).toHaveBeenCalledWith('7', {
      environment_version_id: 11,
      import_policy_mode: 'unrestricted',
      allowed_imports: [],
    })
  })

  it('创建题目默认继承作业：签名自动解析函数名，环境 null', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '继承题')
    // 签名解析出函数名提示
    expect(wrapper.text()).toContain('已识别函数名「add」')
    await clickBtn(wrapper, '保存题目')
    await flushPromises()

    expect(assignmentsAPI.createQuestion).toHaveBeenCalledWith('7', expect.objectContaining({
      function_name: 'add',
      environment_version_id: null,
      import_policy_mode: 'inherit',
      allowed_imports: [],
    }))
  })

  it('签名无法解析时提示且阻止提交', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '坏签名', 'hello world')
    expect(wrapper.text()).toContain('未识别到 def 签名')
    await clickBtn(wrapper, '保存题目')
    await flushPromises()
    expect(assignmentsAPI.createQuestion).not.toHaveBeenCalled()
    expect(appStore.showToast).toHaveBeenCalledWith('请填写标题和函数名', 'error')
  })

  it('题目级运行设置已移除：不再提供环境/导入规则覆盖，统一继承作业', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '继承题')

    // 顶部作业配置架只保留作业默认环境 + 发布范围，没有「本题运行设置」
    expect(wrapper.find('.qe-side').text()).not.toContain('本题运行设置')
    expect(wrapper.find('.qe-side').text()).not.toContain('指定环境')
    expect(wrapper.find('.qe-side').text()).not.toContain('自定义白名单')
    // 超时/内存收敛到题目编辑区的「运行参数」卡
    expect(wrapper.find('.qe-run-card').exists()).toBe(true)
    expect(wrapper.find('.qe-run-card').text()).toContain('超时 (ms)')
    expect(wrapper.find('.qe-run-card').text()).toContain('内存限制 (MB)')

    await clickBtn(wrapper, '保存题目')
    await flushPromises()

    expect(assignmentsAPI.createQuestion).toHaveBeenCalledWith('7', expect.objectContaining({
      environment_version_id: null,
      import_policy_mode: 'inherit',
      allowed_imports: [],
    }))
  })

  it('题目内存低于环境最低值显示警告卡，「使用推荐值」一键恢复', async () => {
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '内存题')
    const memInputs = wrapper.findAll('input[type="number"]')
    await memInputs[1].setValue(128)  // 内存限制 128 < 环境最低 256
    await flushPromises()
    expect(wrapper.text()).toContain('内存上限 128 MB 低于环境最低内存 256 MB')
    // 警告卡片 + 推荐按钮
    expect(wrapper.find('.qe-warn-card').exists()).toBe(true)
    await clickBtn(wrapper, '使用推荐值')
    await flushPromises()
    expect(memInputs[1].element.value).toBe('256')
    expect(wrapper.find('.qe-warn-card').exists()).toBe(false)
  })

  it('编辑已有题目回填并调用 updateQuestion', async () => {
    assignmentsAPI.getQuestions.mockResolvedValue({
      data: {
        items: [{
          id: 55, assignment_id: 7, title: '旧题', function_name: 'old', hidden_tests: 'x',
          time_limit_ms: 10000, memory_limit_mb: 512, grading_mode: 'legacy',
          environment_version_id: 22, import_policy_mode: 'restricted', allowed_imports: ['numpy'],
        }],
      },
    })
    assignmentsAPI.updateQuestion.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()

    await wrapper.find('.qe-list-actions button').trigger('click')  // 列表「编辑」按钮
    await flushPromises()
    expect(wrapper.find('input[placeholder="如: 两数之和"]').element.value).toBe('旧题')
    // 回填后更新标题并保存
    await wrapper.find('input[placeholder="如: 两数之和"]').setValue('新题')
    await clickBtn(wrapper, '保存题目')
    await flushPromises()

    expect(assignmentsAPI.updateQuestion).toHaveBeenCalledWith('7', 55, expect.objectContaining({
      title: '新题',
      // 题目级覆盖入口已移除：编辑保存时也统一回写为继承作业
      environment_version_id: null,
      import_policy_mode: 'inherit',
      allowed_imports: [],
    }))
  })

  it('表格化公开样例：添加/行内编辑后序列化为原 JSON 数组格式', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '求和题')

    // 添加样例 → 行内编辑（2 个参数）
    await clickBtn(wrapper, '+ 添加样例')
    await clickBtn(wrapper, '+ 参数')
    await clickBtn(wrapper, '+ 参数')
    const argInputs = wrapper.findAll('.qe-cases__input--arg')
    await argInputs[0].setValue('1')
    await argInputs[1].setValue('2')
    await wrapper.find('input[placeholder="如 3、ok 或 [1,2]"]').setValue('3')
    await wrapper.find('input[placeholder="说明（可选）"]').setValue('正数相加')
    await wrapper.find('.qe-cases__op--save').trigger('click')
    await flushPromises()

    // 表格展示行
    expect(wrapper.text()).toContain('正数相加')

    // 保存后 public_cases 仍为原 JSON 数组格式
    await clickBtn(wrapper, '保存题目')
    await flushPromises()
    expect(assignmentsAPI.createQuestion).toHaveBeenCalledWith('7', expect.objectContaining({
      public_cases: [{ args: [1, 2], expected: 3, desc: '正数相加' }],
    }))
  })

  it('批量导入公开样例并分页（每页 10 条，内部滚动不撑高页面）', async () => {
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '分页题')

    await clickBtn(wrapper, '批量导入')
    const cases = Array.from({ length: 12 }, (_, i) => ({ args: [i], expected: i + 1 }))
    await wrapper.find('.qe-cases__import-ta').setValue(JSON.stringify(cases))
    await clickBtn(wrapper, '导入追加')
    await flushPromises()

    // 导入后跳到末页（新数据可见）
    expect(wrapper.text()).toContain('共 12 条 / 显示 11–12')
    // 上一页回到第一页
    await wrapper.findAll('.qe-cases__pager-btn')[0].trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('显示 1–10')
    // 表格区域有固定可视高度（内部滚动）
    expect(wrapper.find('.qe-cases__table-wrap').exists()).toBe(true)
  })

  it('私有测试可视化表格：保存为 pytest 代码（hidden_tests 契约不变）', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '私有题')

    await clickBtn(wrapper, '私有测试')
    await clickBtn(wrapper, '+ 添加用例')
    await clickBtn(wrapper, '+ 参数')
    await wrapper.findAll('.qe-cases__input--arg')[0].setValue('3')
    await wrapper.find('input[placeholder="如 3、ok 或 [1,2]"]').setValue('6')
    await wrapper.find('.qe-cases__op--save').trigger('click')
    await flushPromises()

    await clickBtn(wrapper, '保存题目')
    await flushPromises()
    expect(assignmentsAPI.createQuestion).toHaveBeenCalledWith('7', expect.objectContaining({
      hidden_tests: expect.stringContaining('def test_case_1()'),
    }))
    const payload = assignmentsAPI.createQuestion.mock.calls[0][1]
    expect(payload.hidden_tests).toContain('assert add(3) == 6')
  })

  it('私有测试回填：已有 pytest 代码解析为可视化表格（同块多断言拆分为多行）', async () => {
    assignmentsAPI.getQuestions.mockResolvedValue({
      data: {
        items: [{
          id: 55, assignment_id: 7, title: '旧题', function_name: 'add', hidden_tests:
            'def test_add():\n    assert add(1, 2) == 3\n    assert add(-1, 1) == 0',
          time_limit_ms: 10000, memory_limit_mb: 512, grading_mode: 'legacy',
          environment_version_id: null, import_policy_mode: 'inherit', allowed_imports: [],
        }],
      },
    })
    const wrapper = await mountPage()
    await flushPromises()
    await wrapper.find('.qe-list-actions button').trigger('click')
    await flushPromises()

    await clickBtn(wrapper, '私有测试')
    // 解析成功进入可视化模式：两条用例拆成两行，参数无括号残留
    expect(wrapper.text()).toContain('共 2 条 / 显示 1–2')
    expect(wrapper.text()).toContain('2')
    expect(wrapper.text()).not.toContain('2)')
  })

  it('运行测试调用 sample-run；教师端 403 时如实展示后端错误', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({ data: { id: 99 } })
    judgeAPI.sampleRun.mockRejectedValue({
      response: { data: { detail: { message: '只有学生可以使用 sample-run' } } },
    })
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '运行题')
    // 保存后会刷新列表并清空为下一题；先让刷新接口返回刚创建的题目
    assignmentsAPI.getQuestions.mockResolvedValue({
      data: {
        items: [{
          id: 99, assignment_id: 7, title: '运行题', function_name: 'add', hidden_tests: '',
          time_limit_ms: 10000, memory_limit_mb: 256, grading_mode: 'legacy',
          environment_version_id: null, import_policy_mode: 'inherit', allowed_imports: [],
        }],
      },
    })
    await clickBtn(wrapper, '保存题目')
    await flushPromises()

    // 保存后自动进入下一题空白表单；从上方列表重新打开刚创建的题目
    expect(wrapper.find('.qe-list-title').text()).toContain('运行题')
    await wrapper.find('.qe-list-actions button').trigger('click')
    await flushPromises()

    await clickBtn(wrapper, '▶ 运行测试')
    await flushPromises()
    expect(judgeAPI.sampleRun).toHaveBeenCalledWith(99, { question_id: 99, code: '' })
    expect(wrapper.text()).toContain('只有学生可以使用 sample-run')
  })

  it('学生代码模板：深色 CodeMirror 编辑器，固定高度不随代码增长', async () => {
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '代码题')

    // 等待 CodeMirror 动态加载完成
    await vi.waitFor(() => {
      expect(wrapper.find('.qe-code .cm-content').exists()).toBe(true)
    }, { timeout: 5000 })

    // 固定高度（默认 380px，长代码内部滚动）
    expect(wrapper.find('.qe-code').attributes('style')).toContain('380px')
    expect(wrapper.find('.qe-code .cm-editor').exists()).toBe(true)
  })

  // ══ 作业级设置架 + 题目编辑区下方 AI 评分配置 ═══════════════════════
  it('顶部只保留作业级运行设置，AI 评分配置不再作为 tab', async () => {
    const wrapper = await mountPage()
    await flushPromises()
    expect(wrapper.find('.qe-side-head').text()).toContain('运行设置')
    expect(wrapper.find('.qe-side-tab').exists()).toBe(false)
    expect(wrapper.find('.qe-side').text()).not.toContain('AI 评分配置')
    expect(wrapper.find('.qe-side').text()).not.toContain('发布设置')
    expect(wrapper.find('.qe-side').text()).not.toContain('前往作业管理')
    // 无题目时自动进入新题编辑态，AI 配置卡直接位于题目编辑区下方
    expect(wrapper.find('#ai-config-section').exists()).toBe(true)
    expect(wrapper.find('#ai-config-section').text()).toContain('AI 评分配置')
  })

  it('新建题目（无 id）时 AI 评分配置直接渲染在题目下方，草稿可编辑', async () => {
    const wrapper = await mountPage()
    await flushPromises()
    // 不再显示「请先保存题目后再配置 AI 评分」
    expect(wrapper.text()).not.toContain('请先保存题目后再配置 AI 评分')
    // 草稿表单渲染且可编辑
    const draft = wrapper.find('.qe-ai-draft')
    expect(draft.exists()).toBe(true)
    expect(draft.text()).toContain('评分模式')
    await draft.find('select').setValue('shadow')
    await flushPromises()
    expect(draft.find('select').element.value).toBe('shadow')
    // 草稿模式无 rubric 区，提示保存题目后可生成 Rubric
    expect(draft.text()).toContain('保存题目后可生成 Rubric')
    expect(draft.text()).not.toContain('AI 生成 Rubric')
  })

  it('草稿模式点击「AI 生成测试组」提示先保存题目（无 questionId 不请求）', async () => {
    const wrapper = await mountPage()
    await flushPromises()
    const draft = wrapper.find('.qe-ai-draft')
    const genBtn = draft.findAll('button').find((b) => b.text().includes('AI 生成测试组'))
    await genBtn.trigger('click')
    await flushPromises()
    expect(draft.text()).toContain('请先保存题目后再生成测试组')
    expect(aiGradingAPI.generateTestGroups).not.toHaveBeenCalled()
  })

  it('新建题草稿选 shadow/active 时显示 Rubric 门禁前置提示', async () => {
    const wrapper = await mountPage()
    await flushPromises()
    // legacy 默认无提示
    expect(wrapper.find('.qe-ai-draft .qe-warn-card').exists()).toBe(false)
    await wrapper.find('.qe-ai-draft select').setValue('shadow')
    await flushPromises()
    expect(wrapper.find('.qe-ai-draft .qe-warn-card').exists()).toBe(true)
    expect(wrapper.text()).toContain('尚无可发布的 Rubric，请先保存题目后生成并锁定')
  })

  it('发布前当前题 shadow/active 时提示 Rubric 门禁（toast + 确认弹窗）', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({ data: { id: 99 } })
    assignmentsAPI.publish.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '门禁题')
    await wrapper.find('.qe-ai-draft select').setValue('shadow')
    await clickBtn(wrapper, '发布作业')
    await flushPromises()
    // toast 前置提示
    expect(appStore.showToast).toHaveBeenCalledWith(expect.stringContaining('Rubric 未生成并锁定'), 'error')
    // 确认弹窗 message 同样拼接门禁提示
    expect(wrapper.text()).toContain('若 Rubric 未生成并锁定，发布将被后端拒绝')
  })

  it('编辑已有题目时 AI 评分配置位于主编辑区，内嵌 AIQuestionConfig（kind=assignment）', async () => {
    assignmentsAPI.getQuestions.mockResolvedValue({
      data: {
        items: [{
          id: 55, assignment_id: 7, title: '旧题', function_name: 'old', hidden_tests: '',
          time_limit_ms: 10000, memory_limit_mb: 512, grading_mode: 'legacy',
          environment_version_id: null, import_policy_mode: 'inherit', allowed_imports: [],
        }],
      },
    })
    const wrapper = await mountPage()
    await flushPromises()
    await wrapper.find('.qe-list-actions button').trigger('click')  // 列表「编辑」按钮
    await flushPromises()
    const cfg = wrapper.findComponent({ name: 'AIQuestionConfig' })
    expect(cfg.exists()).toBe(true)
    expect(cfg.props('kind')).toBe('assignment')
    expect(cfg.props('questionId')).toBe(55)
    expect(cfg.props('expanded')).toBe(true)
    // 内嵌在题目编辑区时关闭「收起」按钮
    expect(cfg.props('closable')).toBe(false)
    // 只挂载在题目编辑区，不在顶部作业设置架
    expect(wrapper.find('#ai-config-section .ai-config-stub').exists()).toBe(true)
    expect(wrapper.find('.qe-side .ai-config-stub').exists()).toBe(false)
  })

  it('新建题保存时 AI 草稿字段随 payload 提交（新建默认显式传 legacy）', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({ data: { id: 99 } })
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, 'AI 草稿题')

    const draft = wrapper.find('.qe-ai-draft')
    await draft.find('select').setValue('shadow')
    await draft.find('[data-testid="teacher-constraints-input"]')
      .setValue('  必须正确处理空列表\n禁止使用全局变量  ')
    await clickBtn(wrapper, '+ 添加测试组')
    await flushPromises()
    await wrapper.findAll('input[placeholder="ID (如 F1)"]')[0].setValue('F1')
    await wrapper.findAll('input[placeholder="名称"]')[0].setValue('基础')
    await wrapper.findAll('input[placeholder="满分"]')[0].setValue('60')

    await clickBtn(wrapper, '保存题目')
    await flushPromises()
    expect(assignmentsAPI.createQuestion).toHaveBeenCalledWith('7', expect.objectContaining({
      grading_mode: 'shadow',
      teacher_constraints: {
        requirements_text: '必须正确处理空列表\n禁止使用全局变量',
      },
      reference_solution: null,
      test_groups: [expect.objectContaining({ id: 'F1', name: '基础', dimension: 'F', max_score: 60 })],
      score_cap_rules: [],
    }))
  })

  it('新建题未碰 AI 配置时保存，payload 显式传 legacy（不依赖后端默认 active）', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({ data: { id: 99 } })
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '默认题')
    await clickBtn(wrapper, '保存题目')
    await flushPromises()
    expect(assignmentsAPI.createQuestion).toHaveBeenCalledWith('7', expect.objectContaining({
      grading_mode: 'legacy',
      teacher_constraints: {},
      test_groups: [],
      score_cap_rules: [],
    }))
  })

  it('创建成功后列表刷新并清空为下一题；修改过的 AI 草稿经 PUT 落库', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({ data: { id: 99 } })
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '持久化题')
    await wrapper.find('.qe-ai-draft select').setValue('shadow')

    // 保存后的刷新接口返回刚创建的题目，用于验证列表已更新
    assignmentsAPI.getQuestions.mockResolvedValue({
      data: {
        items: [{
          id: 99, assignment_id: 7, title: '持久化题', function_name: 'add', hidden_tests: '',
          time_limit_ms: 10000, memory_limit_mb: 256, grading_mode: 'shadow',
          environment_version_id: null, import_policy_mode: 'inherit', allowed_imports: [],
        }],
      },
    })
    await clickBtn(wrapper, '保存题目')
    await flushPromises()
    // 草稿经独立接口落库到新题目（create 链路不接收 AI 字段）
    expect(aiGradingAPI.updateConfig).toHaveBeenCalledWith('assignment', 99, expect.objectContaining({ grading_mode: 'shadow' }))
    // 列表刷新后展示刚创建的题目，且下方自动重置为新题草稿（默认 legacy）
    expect(wrapper.find('.qe-list-title').text()).toContain('持久化题')
    expect(wrapper.find('.qe-ai-draft').exists()).toBe(true)
    expect(wrapper.find('.qe-ai-draft select').element.value).toBe('legacy')
    expect(wrapper.findComponent({ name: 'AIQuestionConfig' }).exists()).toBe(false)
    expect(appStore.showToast).not.toHaveBeenCalledWith(expect.stringContaining('已丢弃'), 'error')
  })

  it('草稿模式教师要求仅含空白时提交空 dict', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({ data: { id: 99 } })
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '空约束题')
    await wrapper.find('.qe-ai-draft [data-testid="teacher-constraints-input"]').setValue('  \n  ')
    await clickBtn(wrapper, '保存题目')
    await flushPromises()
    expect(assignmentsAPI.createQuestion).toHaveBeenCalledWith('7', expect.objectContaining({
      teacher_constraints: {},
    }))
  })

  it('新建题 AI 草稿未保存时切换题目：提示已丢弃且草稿重置', async () => {
    assignmentsAPI.getQuestions.mockResolvedValue({
      data: {
        items: [{
          id: 55, assignment_id: 7, title: '旧题', function_name: 'old', hidden_tests: '',
          time_limit_ms: 10000, memory_limit_mb: 512, grading_mode: 'legacy',
          environment_version_id: null, import_policy_mode: 'inherit', allowed_imports: [],
        }],
      },
    })
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '草稿切题')
    await wrapper.find('.qe-ai-draft select').setValue('shadow')

    await wrapper.find('.qe-list-actions button').trigger('click')  // 切换到已有题
    await flushPromises()
    expect(appStore.showToast).toHaveBeenCalledWith(expect.stringContaining('AI 配置草稿未保存'), 'error')

    // 再次新建：草稿已重置为默认 legacy
    await clickBtn(wrapper, '添加题目')
    await flushPromises()
    expect(wrapper.find('.qe-ai-draft select').element.value).toBe('legacy')
  })

  it('列表行「AI 配置」收敛：选中该题并滚动到题目编辑区下方的 AI 配置（无双实例）', async () => {
    assignmentsAPI.getQuestions.mockResolvedValue({
      data: {
        items: [{
          id: 55, assignment_id: 7, title: '旧题', function_name: 'old', hidden_tests: '',
          time_limit_ms: 10000, memory_limit_mb: 512, grading_mode: 'legacy',
          environment_version_id: null, import_policy_mode: 'inherit', allowed_imports: [],
        }],
      },
    })
    const wrapper = await mountPage()
    await flushPromises()
    await clickBtn(wrapper, 'AI 配置')
    await flushPromises()
    // 该题被选中（编辑态），AIQuestionConfig 只挂载在题目编辑区下方
    expect(wrapper.find('.qe-list-row--active').exists()).toBe(true)
    // 列表行内不再挂载 AIQuestionConfig，仅题目编辑区一个实例
    expect(wrapper.find('.qe-list-row .ai-config-stub').exists()).toBe(false)
    expect(wrapper.find('#ai-config-section .ai-config-stub').exists()).toBe(true)
    expect(wrapper.find('.qe-side .ai-config-stub').exists()).toBe(false)
  })

  it('已有题 AI 配置保存成功：刷新上方列表并清空下方进入下一题', async () => {
    assignmentsAPI.getQuestions.mockResolvedValue({
      data: {
        items: [{
          id: 55, assignment_id: 7, title: '旧题', function_name: 'old', hidden_tests: '',
          time_limit_ms: 10000, memory_limit_mb: 512, grading_mode: 'legacy',
          environment_version_id: null, import_policy_mode: 'inherit', allowed_imports: [],
        }],
      },
    })
    const wrapper = await mountPage()
    await flushPromises()
    await wrapper.find('.qe-list-actions button').trigger('click')
    await flushPromises()
    const cfg = wrapper.findComponent({ name: 'AIQuestionConfig' })
    expect(cfg.exists()).toBe(true)

    cfg.vm.$emit('saved', 55)
    await flushPromises()
    // 触发列表刷新 + 清空为下一题空白表单
    expect(assignmentsAPI.getQuestions.mock.calls.length).toBeGreaterThanOrEqual(2)
    expect(wrapper.find('.qe-ai-draft').exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'AIQuestionConfig' }).exists()).toBe(false)
    expect(appStore.showToast).toHaveBeenCalledWith('AI 配置已保存，已进入下一题', 'success')
  })

  // ══ 右上角「🚀 发布作业」 ═════════════════════════════════════════
  it('右上角发布作业：确认后先保存当前题目，再调用 publish 接口', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({ data: { id: 99 } })
    assignmentsAPI.publish.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '发布题')
    await clickBtn(wrapper, '发布作业')
    await flushPromises()
    // 确认弹窗 → 确认发布
    await clickBtn(wrapper, '确认发布')
    await flushPromises()
    expect(assignmentsAPI.createQuestion).toHaveBeenCalled()
    // 批次 4：发布作业携带「计入课程总评 + 分类」选择（默认勾选，预选平时）
    expect(assignmentsAPI.publish).toHaveBeenCalledWith('7', { gradebook_binding: { enabled: true, category_key: 'regular' } })
    expect(appStore.showToast).toHaveBeenCalledWith('作业已发布', 'success')
  })

  it('发布作业：题目保存失败时停止，不调用 publish', async () => {
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '坏签名题', 'hello world')  // 签名无法解析 → 保存失败
    await clickBtn(wrapper, '发布作业')
    await flushPromises()
    await clickBtn(wrapper, '确认发布')
    await flushPromises()
    expect(assignmentsAPI.createQuestion).not.toHaveBeenCalled()
    expect(assignmentsAPI.publish).not.toHaveBeenCalled()
  })

  it('发布作业：接口失败时明确提示题目已保存为草稿，但发布失败', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({ data: { id: 99 } })
    assignmentsAPI.publish.mockRejectedValue({
      response: { data: { detail: { message: '存在未配置 AI 评分的 legacy 题目' } } },
    })
    const wrapper = await mountPage()
    await flushPromises()
    await startNewQuestion(wrapper, '发布失败题')
    await clickBtn(wrapper, '发布作业')
    await flushPromises()
    await clickBtn(wrapper, '确认发布')
    await flushPromises()
    expect(assignmentsAPI.createQuestion).toHaveBeenCalled()
    // 批次 4：发布作业携带「计入课程总评 + 分类」选择（默认勾选，预选平时）
    expect(assignmentsAPI.publish).toHaveBeenCalledWith('7', { gradebook_binding: { enabled: true, category_key: 'regular' } })
    expect(appStore.showToast).toHaveBeenCalledWith(expect.stringContaining('题目已保存为草稿，但发布失败'), 'error')
    expect(appStore.showToast).toHaveBeenCalledWith(expect.stringContaining('存在未配置 AI 评分的 legacy 题目'), 'error')
  })

  it('作业已发布时右上角按钮显示已发布并禁用', async () => {
    assignmentsAPI.get.mockResolvedValue({ data: { ...assignmentDraft, status: 'published' } })
    const wrapper = await mountPage()
    await flushPromises()
    const btn = wrapper.findAll('.qe-topbar-right button').find((b) => b.text().includes('已发布'))
    expect(btn).toBeTruthy()
    expect(btn.attributes('disabled')).toBeDefined()
    await btn.trigger('click')
    await flushPromises()
    expect(assignmentsAPI.publish).not.toHaveBeenCalled()
  })

  // ══ 底部操作栏精简 ═══════════════════════════════════════════════
  it('底部操作栏精简：只剩取消与保存题目（保存草稿/发布题目已移除）', async () => {
    assignmentsAPI.createQuestion.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    expect(wrapper.text()).not.toContain('保存草稿')
    expect(wrapper.text()).not.toContain('发布题目')
    await startNewQuestion(wrapper, '精简题')
    await clickBtn(wrapper, '保存题目')
    await flushPromises()
    expect(assignmentsAPI.createQuestion).toHaveBeenCalled()
    expect(wrapper.text()).toContain('取消')
  })

  // ══ TASK-017 题目删除 ════════════════════════════════════════════
  const questionRows = {
    items: [
      { id: 42, title: '待删题', function_name: 'add', time_limit_ms: 10000, memory_limit_mb: 256, grading_mode: 'legacy' },
    ],
  }

  it('草稿作业题目行显示删除按钮，确认后调用 deleteQuestion 并刷新列表', async () => {
    assignmentsAPI.getQuestions.mockResolvedValue({ data: questionRows })
    assignmentsAPI.deleteQuestion.mockResolvedValue({})
    const wrapper = await mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('待删题')
    await clickBtn(wrapper, '删除')
    await flushPromises()
    // 确认弹窗出现
    const dialog = wrapper.find('.confirm-panel')
    expect(dialog.exists()).toBe(true)
    expect(dialog.text()).toContain('删除题目')
    expect(dialog.text()).toContain('待删题')
    await clickBtn(dialog, '确认删除')
    await flushPromises()
    expect(assignmentsAPI.deleteQuestion).toHaveBeenCalledWith('7', 42)
    expect(appStore.showToast).toHaveBeenCalledWith('题目已删除', 'success')
    // 删除后重新拉取题目列表
    expect(assignmentsAPI.getQuestions.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('删除失败时展示后端错误信息', async () => {
    assignmentsAPI.getQuestions.mockResolvedValue({ data: questionRows })
    assignmentsAPI.deleteQuestion.mockRejectedValue({
      response: { data: { detail: { message: '该作业已有学生提交，评分输入与规则不可修改' } } },
    })
    const wrapper = await mountPage()
    await flushPromises()
    await clickBtn(wrapper, '删除')
    await flushPromises()
    await clickBtn(wrapper.find('.confirm-panel'), '确认删除')
    await flushPromises()
    expect(appStore.showToast).toHaveBeenCalledWith(
      '该作业已有学生提交，评分输入与规则不可修改', 'error',
    )
  })

  it('已发布作业的题目行不显示删除按钮', async () => {
    assignmentsAPI.get.mockResolvedValue({ data: { ...assignmentDraft, status: 'published' } })
    assignmentsAPI.getQuestions.mockResolvedValue({ data: questionRows })
    const wrapper = await mountPage()
    await flushPromises()
    const delBtns = wrapper.findAll('button').filter((b) => b.text().includes('删除'))
    expect(delBtns.length).toBe(0)
  })

  // ══ TASK-020 AI 治理门横幅 ═══════════════════════════════════════
  it('AI 已启用且 Key 齐备时不显示治理横幅', async () => {
    aiGradingAPI.getStatus.mockResolvedValue({ data: { enabled: true, ready: true } })
    const wrapper = await mountPage()
    await flushPromises()
    expect(wrapper.find('.qe-ai-notice').exists()).toBe(false)
  })

  it('AI 未启用（未审批）时显示治理横幅', async () => {
    aiGradingAPI.getStatus.mockResolvedValue({ data: { enabled: false, ready: false } })
    const wrapper = await mountPage()
    await flushPromises()
    const notice = wrapper.find('.qe-ai-notice')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('未完成数据治理审批')
  })

  it('AI 已启用但缺 Key 时显示配置提示横幅', async () => {
    aiGradingAPI.getStatus.mockResolvedValue({ data: { enabled: true, ready: false } })
    const wrapper = await mountPage()
    await flushPromises()
    const notice = wrapper.find('.qe-ai-notice')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('未配置 API Key')
  })
})
