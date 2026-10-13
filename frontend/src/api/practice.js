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

/**
 * 练习榜单与个人统计（批次 6）：榜单快照（含 opt-in 状态与「我的名次 ±3」）、
 * 个人统计/打卡日历/徽章墙/成长曲线、全平台榜加入退出、管理员重算。
 */
export const practiceSocialAPI = {
  /** 榜单快照：scope=platform|offering、board=deterministic|ai、view=top|me */
  leaderboard(params) { return client.get('/practice/social/leaderboard', { params }) },
  /** 学生个人面板（统计卡 + 打卡 + 徽章 + 近 30 天成长曲线） */
  getOverview() { return client.get('/practice/social/me/overview') },
  getOptIn() { return client.get('/practice/social/me/opt-in') },
  setOptIn(optIn) { return client.put('/practice/social/me/opt-in', { opt_in: optIn }) },
  /** 管理员手动重算（幂等）；返回 {students, platform_rows, offering_rows, skipped} */
  recalculate() { return client.post('/practice/social/recalculate') },
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
