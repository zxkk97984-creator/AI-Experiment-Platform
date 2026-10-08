import client from './client.js'

const entry = (kind, id, offeringId) => offeringId ? `/offerings/${offeringId}/experiments/${kind}/${id}/assessment` : `/labs/entries/${kind}/${id}/assessment`
// Axios supplies the multipart boundary; never serialize an uploaded File as JSON.
const multipart = { headers: { 'Content-Type': 'multipart/form-data' }, timeout: 600000 }
export const labsAPI = {
  checkCell: (id, cellId, payload) => client.post(`/labs/records/${id}/cells/${encodeURIComponent(cellId)}/check`, payload),
  leaderboard: (id) => client.get(`/labs/records/${id}/leaderboard`),
  uploadCheckScript(kind, id, file, offeringId) {
    const form = new FormData()
    form.append('file', file)
    return client.post(`${entry(kind, id, offeringId)}/check-script`, form, multipart)
  },
  getRegradeBatch(id) { return client.get(`/labs/regrade-batches/${id}`) },
  artifactFiles(id, page = 1) { return client.get(`/labs/artifacts/${id}/files`, { params: { page, page_size: 50 } }) },
  runtime: () => client.get('/labs/runtime'),
  getAssessment: (kind, id, offeringId) => client.get(entry(kind, id, offeringId)),
  saveAssessment: (kind, id, config, offeringId) => client.put(entry(kind, id, offeringId), config),
  generateRubric: (kind, id, config) => client.post(`${entry(kind, id)}/generate-rubric`, config, { timeout: 200000 }),
  baseline: (kind, id, offeringId) => client.post(`${entry(kind, id, offeringId)}/baseline`),
  publish: (kind, id, offeringId) => client.post(`${entry(kind, id, offeringId)}/publish`),
  regradeBatch: (kind, id, payload, offeringId) => client.post(`${entry(kind, id, offeringId)}/regrade`, payload),
  listDatasets: (params) => client.get('/labs/datasets', { params }),
  uploadDataset(file, name, taskType, config) {
    const form = new FormData()
    form.append('file', file)
    form.append('name', name)
    form.append('task_type', taskType)
    form.append('config_json', JSON.stringify(config))
    return client.post('/labs/datasets', form, multipart)
  },
  getDataset: (id) => client.get(`/labs/datasets/${id}`),
  getOverview: (id) => client.get(`/labs/records/${id}/overview`),
  train: (id, payload) => client.post(`/labs/records/${id}/train`, payload),
  uploadArtifact(id, file, revision) {
    const form = new FormData()
    form.append('file', file)
    form.append('expected_record_revision', String(revision))
    return client.post(`/labs/records/${id}/artifacts`, form, multipart)
  },
  uploadReference(kind, id, file) {
    const form = new FormData()
    form.append('file', file)
    return client.post(`/labs/entries/${kind}/${id}/reference-artifacts`, form, multipart)
  },
  validate: (id, payload) => client.post(`/labs/records/${id}/validate`, payload),
  getRun: (id) => client.get(`/labs/runs/${id}`),
  cancelRun: (id) => client.post(`/labs/runs/${id}/cancel`),
  deleteArtifact: (id) => client.delete(`/labs/artifacts/${id}`),
  getFile: (id) => client.get(`/labs/files/${id}`, { responseType: 'blob' }),
  getSubmission: (id) => client.get(`/labs/submissions/${id}`),
  review: (id, payload) => client.post(`/labs/submissions/${id}/review`, payload),
  appeal: (id, reason) => client.post(`/labs/submissions/${id}/appeal`, { reason }),
  regrade: (id, reason) => client.post(`/labs/submissions/${id}/regrade`, { reason }),
  starter: (taskType) => client.get(`/labs/starter/${taskType}`),
}
