import client from './client.js'

// 评分版本编辑（批次三 3C）：教师维护"可信数据判题"版本的读写契约。
// 所有端点都要求教师/管理员，且必须在该作业/考试的授课范围内（后端强制）。
//
// 作业题与考试题的端点前缀不同（权限范围与题目表也不同），用 kind 统一分派：
// 面板必须传 kind，否则考试题会打到 /judge 的作业资源上（可能操作同 ID 的其他题目）。

function base(kind) {
  return kind === 'exam' ? '/exams' : '/judge'
}

export const gradingVersionsAPI = {
  list(questionId, kind = 'assignment') {
    return client.get(`${base(kind)}/questions/${questionId}/grading-versions`)
  },
  createDraft(questionId, payload, kind = 'assignment') {
    return client.post(`${base(kind)}/questions/${questionId}/grading-versions`, payload)
  },
  detail(versionId, kind = 'assignment') {
    return client.get(`${base(kind)}/grading-versions/${versionId}`)
  },
  confirm(versionId, expectedSpecDigest, kind = 'assignment', confirmIsolatedCaseV2 = false) {
    const body = {
      ...(expectedSpecDigest ? { expected_spec_digest: expectedSpecDigest } : {}),
      confirm_isolated_case_v2: confirmIsolatedCaseV2,
    }
    return client.post(`${base(kind)}/grading-versions/${versionId}/confirm`, body)
  },
  activate(versionId, { acknowledgeInFlight = false, kind = 'assignment' } = {}) {
    const query = acknowledgeInFlight ? '?acknowledge_in_flight=true' : ''
    return client.post(`${base(kind)}/grading-versions/${versionId}/activate${query}`)
  },
  retire(versionId, kind = 'assignment') {
    return client.post(`${base(kind)}/grading-versions/${versionId}/retire`)
  },
  // 启用前影响面核查：在途任务 / 无基线历史记录 / 进行中的考试场次
  preflight(questionId, kind = 'assignment') {
    return client.get(`${base(kind)}/questions/${questionId}/grading-versions/preflight`)
  },
  // 预检：只读，不写库、不执行题目源码
  convertPreview(questionId, body = {}, kind = 'assignment') {
    return client.post(`${base(kind)}/questions/${questionId}/grading-versions/convert-preview`, body)
  },
  // 应用：把可转换用例落成草稿（仍需确认与启用）
  convertApply(questionId, body = {}, kind = 'assignment') {
    return client.post(`${base(kind)}/questions/${questionId}/grading-versions/convert-apply`, body)
  },
  createAiDraft(questionId, body, kind = 'assignment') {
    return client.post(`${base(kind)}/questions/${questionId}/grading-versions/ai-draft`, body)
  },
  // AI 生成 → AST 预检 → 草稿（仅作业题：考试题没有 function_name，无法安全生成）
  aiGenerate(questionId, body = {}) {
    return client.post(`/judge/questions/${questionId}/grading-versions/ai-generate`, body)
  },
  // 逐例表单：字面量 ↔ 协议值（服务端只解析字面量，不执行代码）
  encodeLiteral(literal) {
    return client.post('/judge/grading-versions/literal', { literal })
  },
  decodeLiteral(value) {
    return client.post('/judge/grading-versions/literal/decode', { value })
  },
}

export default gradingVersionsAPI
