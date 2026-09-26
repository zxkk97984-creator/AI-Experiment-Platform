import client from './client.js'

export const accountAPI = {
  profile: () => client.get('/users/me/profile'),
  updateProfile: (data) => client.patch('/users/me/profile', data),
  changePassword: (data) => client.patch('/users/me/password', data),
  policy: () => client.get('/admin/account-security'),
  updatePolicy: (data) => client.put('/admin/account-security', data),
  template: (kind) => client.get('/admin/roster-imports/template', { params: { kind }, responseType: 'blob' }),
  preview: (data) => client.post('/admin/roster-imports/preview', data, { headers: { 'Content-Type': 'multipart/form-data' } }),
  previewReset: (data) => client.post('/admin/password-resets/preview', data),
  batches: (params) => client.get('/admin/batches', { params }),
  batch: (id, params) => client.get(`/admin/batches/${id}`, { params }),
  confirm: (id, data) => client.post(`/admin/batches/${id}/confirm`, data),
  report: (id) => client.get(`/admin/batches/${id}/report`, { responseType: 'blob' }),
  audit: (params) => client.get('/admin/audit-events', { params }),
  history: (params) => client.get('/users/me/history', { params }),
  historyDetail: (kind, id) => client.get(`/users/me/history/${kind}/${id}`),
}

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export const batchStatuses = {
  preview: '待确认', queued: '等待执行', preparing: '正在准备凭据', applying: '正在应用名单',
  succeeded: '已完成', partially_succeeded: '部分完成', failed: '执行失败', conflict: '预检已变化', expired: '已过期',
}
export const batchKinds = { classes: '教学班导入', students: '学生名单导入', class_students: '班内学生导入', password_reset: '学生密码重置' }
export const errorMessage = (e) => e.response?.data?.detail?.message || '操作失败，请稍后重试'
