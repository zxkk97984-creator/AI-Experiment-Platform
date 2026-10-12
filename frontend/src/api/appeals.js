import client from './client.js'

/** 单次成绩异议（批次 2）：学生申请/撤回/补充说明；教师与管理员列表与处理。 */
export const appealsAPI = {
  create(data) { return client.post('/appeals', data) },
  mine() { return client.get('/appeals/mine') },
  withdraw(id) { return client.post(`/appeals/${id}/withdraw`) },
  reply(id, message) { return client.post(`/appeals/${id}/reply`, { message }) },
  list(params) { return client.get('/appeals', { params }) },
  handle(id, data) { return client.post(`/appeals/${id}/handle`, data) },
}
