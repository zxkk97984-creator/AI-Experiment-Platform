import client from './client.js'

export const examsAPI = {
  list(params) { return client.get('/exams', { params }) },
  create(data) { return client.post('/exams', data) },
  get(id) { return client.get(`/exams/${id}`) },
  update(id, data) { return client.patch(`/exams/${id}`, data) },
  getSession(id) { return client.get(`/exams/${id}/session`) },
  importAudienceStudents(examId, kind, file) {
    const formData = new FormData()
    formData.append('file', file)
    return client.post(`/exams/${examId}/audience/import`, formData, { params: { kind } })
  },
  start(id) { return client.post(`/exams/${id}/start`) },
  // 写入代次是必填参数：换机接管后旧设备会被 409 DEVICE_SUPERSEDED 拒绝
  submit(id, expectedWriteGeneration) {
    return client.post(`/exams/${id}/submit`, { expected_write_generation: expectedWriteGeneration })
  },
  takeover(id, data) { return client.post(`/exams/${id}/takeover`, data) },
  getGrades(id, params) { return client.get(`/exams/${id}/grades`, { params }) },
  getGradeDetail(examId, submissionId) { return client.get(`/exams/${examId}/grades/${submissionId}`) },
  getProctor(id, params) { return client.get(`/exams/${id}/proctor`, { params }) },
  updateGradeAnswerScore(examId, submissionId, answerId, score, reason, expectedResultRevision) {
    return client.patch(`/exams/${examId}/grades/${submissionId}/answers/${answerId}/score`, {
      score, reason, expected_result_revision: expectedResultRevision,
    })
  },
  updateGradeQuestionScore(examId, submissionId, questionId, score, reason, expectedResultRevision) {
    return client.patch(`/exams/${examId}/grades/${submissionId}/questions/${questionId}/score`, {
      score, reason, expected_result_revision: expectedResultRevision,
    })
  },
  getQuestions(id) { return client.get(`/exams/${id}/questions`) },
  createQuestion(id, data) { return client.post(`/exams/${id}/questions`, data) },
  updateQuestion(examId, qId, data) { return client.patch(`/exams/${examId}/questions/${qId}`, data) },
  deleteQuestion(examId, qId) { return client.delete(`/exams/${examId}/questions/${qId}`) },
  saveAnswer(examId, qId, data) { return client.put(`/exams/${examId}/answers/${qId}`, data) },
  saveAnswers(examId, answers, expectedWriteGeneration, events) {
    const body = { expected_write_generation: expectedWriteGeneration, answers }
    if (events && events.length) body.events = events
    return client.put(`/exams/${examId}/answers`, body)
  },
  sampleRun(examId, questionId, data) { return client.post(`/exams/${examId}/questions/${questionId}/sample-run`, data) },
  getMyGrade(id) { return client.get(`/exams/${id}/my-grade`) },
  releaseReview(id) { return client.post(`/exams/${id}/review-release`) },
  extendSubmission(examId, submissionId, minutes) {
    return client.patch(`/exams/${examId}/submissions/${submissionId}/extend`, { minutes })
  },
  forceSubmit(examId, submissionId) {
    return client.post(`/exams/${examId}/submissions/${submissionId}/force-submit`)
  },
}
