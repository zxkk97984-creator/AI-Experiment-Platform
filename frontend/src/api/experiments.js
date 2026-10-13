import client from './client.js'

/** 统一的实验 API — 仅此入口，不再调用 /notebooks */
export const experimentsAPI = {
  /** 确保学生有 lesson notebook 记录 */
  ensureForLesson(lessonId, offeringId) {
    return client.post(`/experiments/records/ensure-for-lesson/${lessonId}`, null, { params: offeringId ? { offering_id: offeringId } : {} })
  },

  /** 确保学生有 module 实验记录 */
  ensureForModule(moduleId, offeringId) {
    return client.post(`/experiments/records/ensure-for-module/${moduleId}`, null, { params: offeringId ? { offering_id: offeringId } : {} })
  },

  /** 获取记录详情（含 cells + outputs） */
  getRecordDetail(recordId) {
    return client.get(`/experiments/records/${recordId}`)
  },

  /** 保存 cells 源码（含 record_revision） */
  saveCells(recordId, cells, recordRevision) {
    return client.put(`/experiments/records/${recordId}/cells`, {
      cells,
      record_revision: recordRevision,
    })
  },

  /** 受理一次单元格执行（异步：202 + execution_id；同 client_request_id 幂等） */
  executeCell(recordId, cellId, code, clientRequestId) {
    return client.post(`/experiments/records/${recordId}/cells/${cellId}/execute`, {
      code,
      ...(clientRequestId ? { client_request_id: clientRequestId } : {}),
    })
  },

  /** 查询一次执行的状态与输出（轮询/刷新恢复） */
  getExecution(recordId, executionId) {
    return client.get(`/experiments/records/${recordId}/executions/${executionId}`)
  },

  /** 刷新恢复：查询该记录当前在途执行（无在途任务时 execution 为 null） */
  getActiveExecution(recordId) {
    return client.get(`/experiments/records/${recordId}/executions/active`)
  },

  /** 中断一次执行：排队中直接取消，运行中由 worker 中断当前执行 */
  cancelExecution(recordId, executionId) {
    return client.post(`/experiments/records/${recordId}/executions/${executionId}/cancel`)
  },

  /** 中断 kernel */
  interrupt(recordId) {
    return client.post(`/experiments/records/${recordId}/interrupt`)
  },

  /** 重启 kernel */
  restart(recordId) {
    return client.post(`/experiments/records/${recordId}/restart`)
  },

  /** 获取实验记录列表 */
  listRecords(params) { return client.get('/experiments/records', { params }) },

  // 模块管理
  listModules(params) { return client.get('/experiments/modules', { params }) },
  /** 学生实验目录（模块 + 当前学生学习状态） */
  listStudentCatalog(params) {
    return client.get('/experiments/modules/student-catalog', { params })
  },
  getModule(id) { return client.get(`/experiments/modules/${id}`) },
  createModule(payload) { return client.post('/experiments/modules', payload) },
  ensureModuleTemplate(id) { return client.post(`/experiments/modules/${id}/template`) },
  updateModule(id, payload) { return client.patch(`/experiments/modules/${id}`, payload) },
  /** 发布模块（后端校验模板就绪）；状态变更只能走 publish/unpublish */
  publishModule(id) { return client.post(`/experiments/modules/${id}/publish`) },
  unpublishModule(id) { return client.post(`/experiments/modules/${id}/unpublish`) },

  // 实验提交
  /** 学生提交实验快照——client_request_id 保证幂等 */
  submitRecord(recordId, clientRequestId, options = {}) {
    return client.post(`/experiments/records/${recordId}/submit`, { client_request_id: clientRequestId, ...options })
  },
  /** 查看提交列表 */
  listSubmissions(params) { return client.get('/experiments/submissions', { params }) },
  /** 查看单次提交详情 */
  getSubmission(id) { return client.get(`/experiments/submissions/${id}`) },
  /** 教师评分反馈 */
  updateReview(submissionId, payload) {
    return client.patch(`/experiments/submissions/${submissionId}/review`, payload)
  },
}
