import { beforeEach, describe, expect, it, vi } from 'vitest'
const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }))
vi.mock('../client.js', () => ({ default: client }))
import { labsAPI } from '../labs.js'
import { experimentsAPI } from '../experiments.js'
describe('lab API contracts', () => {
  beforeEach(() => vi.clearAllMocks())
  it('uploads a dataset using multipart with the complete split configuration', () => {
    const file = new File(['sample_id,label\n1,a'], 'data.csv')
    labsAPI.uploadDataset(file, '教学数据', 'tabular_classification', { target_column: 'label', id_column: 'sample_id', seed: 42 })
    const [url, form, options] = client.post.mock.calls[0]
    expect(url).toBe('/labs/datasets')
    expect(form.get('file')).toBe(file)
    expect(JSON.parse(form.get('config_json'))).toEqual({ target_column: 'label', id_column: 'sample_id', seed: 42 })
    expect(options.headers['Content-Type']).toBe('multipart/form-data')
  })
  it('freezes the expected revision alongside an artifact when formally submitting', () => {
    experimentsAPI.submitRecord(5, 'request', { artifact_id: 19, expected_record_revision: 3 })
    expect(client.post).toHaveBeenCalledWith('/experiments/records/5/submit', { client_request_id: 'request', artifact_id: 19, expected_record_revision: 3 })
  })
  it('preserves the original legacy submission payload', () => {
    experimentsAPI.submitRecord(5, 'legacy')
    expect(client.post).toHaveBeenCalledWith('/experiments/records/5/submit', { client_request_id: 'legacy' })
  })
  it('uses the same assessment API for lesson and module entries', () => {
    labsAPI.getAssessment('lesson', 2)
    labsAPI.publish('module', 3)
    expect(client.get).toHaveBeenCalledWith('/labs/entries/lesson/2/assessment')
    expect(client.post).toHaveBeenCalledWith('/labs/entries/module/3/assessment/publish')
  })
  it('scopes configuration, baseline and publish to an offering when supplied', () => {
    labsAPI.getAssessment('module', 3, 11)
    labsAPI.saveAssessment('module', 3, { criteria: [] }, 11)
    labsAPI.baseline('module', 3, 11)
    labsAPI.publish('module', 3, 11)
    expect(client.get).toHaveBeenCalledWith('/offerings/11/experiments/module/3/assessment')
    expect(client.put).toHaveBeenCalledWith('/offerings/11/experiments/module/3/assessment', { criteria: [] })
    expect(client.post).toHaveBeenCalledWith('/offerings/11/experiments/module/3/assessment/baseline')
    expect(client.post).toHaveBeenCalledWith('/offerings/11/experiments/module/3/assessment/publish')
  })
  it.each([
    ['module', 3, 11, '/offerings/11/experiments/module/3/assessment/check-script'],
    ['lesson', 2, 12, '/offerings/12/experiments/lesson/2/assessment/check-script'],
    ['module', 3, undefined, '/labs/entries/module/3/assessment/check-script'],
  ])('uploads a Python check with the correct scope and multipart body (%s)', (kind, id, offeringId, expected) => {
    const file = new File(['pass\n'], 'check.py', { type: 'text/plain' })
    labsAPI.uploadCheckScript(kind, id, file, offeringId)
    const [url, form, options] = client.post.mock.calls[0]
    expect(url).toBe(expected)
    expect(form).toBeInstanceOf(FormData)
    expect(form.get('file')).toBe(file)
    expect(options.headers['Content-Type']).toBe('multipart/form-data')
  })
})
