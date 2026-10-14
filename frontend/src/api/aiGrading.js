import client from './client.js'

export const aiGradingAPI = {
  /** TASK-020：AI 服务状态（enabled=开关，ready=开关+Key 齐备） */
  getStatus() { return client.get('/ai-grading/status') },
  getConfig(kind, id) { return client.get(`/ai-grading/questions/${kind}/${id}/config`) },
  updateConfig(kind, id, data) { return client.put(`/ai-grading/questions/${kind}/${id}/config`, data) },
  listRubrics(kind, id) { return client.get(`/ai-grading/questions/${kind}/${id}/rubrics`) },
  // 单次 DeepSeek 调用（后端 ai_timeout_seconds 默认 60s、上限 180s）；预留 200s，避免后端完成前 axios 先中断
  generateRubric(kind, id) { return client.post(`/ai-grading/questions/${kind}/${id}/rubrics/generate`, null, { timeout: 200000 }) },
  // 生成最多两次 DeepSeek 调用（单次最长 120s）+ Docker 预检；预留 300s，避免后端完成前 axios 先中断
  generateTestGroups(kind, id, data) { return client.post(`/ai-grading/questions/${kind}/${id}/test-groups/generate`, data, { timeout: 300000 }) },
  updateRubric(id, data) { return client.patch(`/ai-grading/rubrics/${id}`, data) },
  lockRubric(id) { return client.post(`/ai-grading/rubrics/${id}/lock`) },
  listGrades(params) { return client.get('/ai-grading/grades', { params }) },
  getGrade(id) { return client.get(`/ai-grading/grades/${id}`) },
  retryGrade(id) { return client.post(`/ai-grading/grades/${id}/retry`) },
  overrideGrade(id, data) { return client.post(`/ai-grading/grades/${id}/override`, data) },
  regradeQuestion(kind, questionId) { return client.post(`/ai-grading/questions/${kind}/${questionId}/regrade`) },

  // ── 一键 AI 配置（批次 2）：异步生成草稿 → 可选试判 → 保存并启用 ──
  createSetupJob(data) { return client.post('/ai-grading/setup-jobs', data) },
  getSetupJob(id) { return client.get(`/ai-grading/setup-jobs/${id}`) },
  saveSetupDraft(id, data) { return client.patch(`/ai-grading/setup-jobs/${id}/draft`, data) },
  retrySetupJob(id) { return client.post(`/ai-grading/setup-jobs/${id}/retry`) },
  createTrialRun(id, data) { return client.post(`/ai-grading/setup-jobs/${id}/trial-runs`, data) },
  getTrialRun(id) { return client.get(`/ai-grading/trial-runs/${id}`) },
  // 单事务保存并启用（内部含用例静态转换），预留 120s 避免网关先于后端中断
  enableSetupJob(id, data) { return client.post(`/ai-grading/setup-jobs/${id}/enable`, data, { timeout: 120000 }) },
}
