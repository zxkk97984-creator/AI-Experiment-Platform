// 角色 → 登录后首页的单一事实来源（登录跳转、守卫回退、侧栏 logo 共用）

export const ROLE_HOME = Object.freeze({
  student: '/student',
  teacher: '/teacher',
  admin: '/admin/users',
})

export function homeForRole(role) {
  return ROLE_HOME[role] || '/login'
}

// 共享页面（教师/管理员复用同一组件）的链接前缀：按当前已鉴权角色计算，
// 服务端授权不变。
export function areaPathForRole(role, path) {
  const prefix = role === 'admin' ? '/admin' : '/teacher'
  return `${prefix}${path.startsWith('/') ? path : `/${path}`}`
}
