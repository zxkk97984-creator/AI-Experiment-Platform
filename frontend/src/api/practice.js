import client from './client.js'

/** 自主练习（批次 5）：学生做题与统计、教师只读排行榜、管理员题库与用量。 */
export const practiceAPI = {
  /** 公共题库：难度 / 标签 / 完成状态筛选 + 搜索 */
  listProblems(params) { return client.get('/practice/problems', { params }) },
  getProblem(id) { return client.get(`/practice/problems/${id}`) },
  /** 提交作答（202）；额度耗尽时后端返回 detail.code = QUOTA_EXHAUSTED */
  submit(problemId, code, clientRequestId) {
    return client.post(`/practice/problems/${problemId}/submissions`, {
      code,
      client_request_id: clientRequestId,
    })
  },
  getSubmission(id) { return client.get(`/practice/submissions/${id}`) },
  /** 我的练习：提交历史与统计 */
  getMine(params) { return client.get('/practice/me', { params }) },
  /** 只读排行榜（教师与学生共用；学生榜由批次 6 的页面调用） */
  leaderboard(params) { return client.get('/practice/leaderboard', { params }) },
}

export const adminPracticeAPI = {
  listProblems(params) { return client.get('/admin/practice/problems', { params }) },
  getProblem(id) { return client.get(`/admin/practice/problems/${id}`) },
  createProblem(data) { return client.post('/admin/practice/problems', data) },
  updateProblem(id, data) { return client.patch(`/admin/practice/problems/${id}`, data) },
  publishProblem(id) { return client.post(`/admin/practice/problems/${id}/publish`) },
  archiveProblem(id) { return client.post(`/admin/practice/problems/${id}/archive`) },
  deleteProblem(id) { return client.delete(`/admin/practice/problems/${id}`) },
  /** 从已有作业复制题目，新建一条草稿题 */
  copyFromAssignment(data) { return client.post('/admin/practice/problems/copy-from-assignment', data) },
  getSettings() { return client.get('/admin/practice/settings') },
  updateSettings(data) { return client.patch('/admin/practice/settings', data) },
  getAiPreflight() { return client.get('/admin/practice/ai-preflight') },
  getUsage(params) { return client.get('/admin/practice/usage', { params }) },
  listAppeals(params) { return client.get('/admin/practice/appeals', { params }) },
  handleAppeal(id, data) { return client.post(`/admin/practice/appeals/${id}/handle`, data) },
}
