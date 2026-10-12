// 批次 2 契约：单次成绩异议端点路径与请求体
import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))

vi.mock('../client.js', () => ({ default: client }))

import { appealsAPI } from '../appeals.js'

describe('appealsAPI', () => {
  beforeEach(() => vi.resetAllMocks())

  it('create 提交目标类型、目标 id 与说明', () => {
    appealsAPI.create({ target_kind: 'submission', target_id: 9, reason: '说明至少二十个字……' })
    const [path, body] = client.post.mock.calls[0]
    expect(path).toBe('/appeals')
    expect(body).toEqual({ target_kind: 'submission', target_id: 9, reason: '说明至少二十个字……' })
  })

  it('mine / list 使用学生与教师不同入口', () => {
    appealsAPI.mine()
    expect(client.get.mock.calls[0][0]).toBe('/appeals/mine')
    appealsAPI.list({ status: 'pending', page: 1, page_size: 20 })
    const [path, config] = client.get.mock.calls[1]
    expect(path).toBe('/appeals')
    expect(config.params).toEqual({ status: 'pending', page: 1, page_size: 20 })
  })

  it('withdraw / reply / handle 使用 POST 子路径', () => {
    appealsAPI.withdraw(3)
    appealsAPI.reply(3, '补充说明')
    appealsAPI.handle(3, { action: 'regrade', comment: '同意重评' })
    expect(client.post.mock.calls[0]).toEqual(['/appeals/3/withdraw'])
    expect(client.post.mock.calls[1]).toEqual(['/appeals/3/reply', { message: '补充说明' }])
    expect(client.post.mock.calls[2]).toEqual(['/appeals/3/handle', { action: 'regrade', comment: '同意重评' }])
  })
})
