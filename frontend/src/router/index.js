import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth.js'
import { homeForRole } from './roleHome.js'

const routes = [
  { path: '/account/password', name: 'ChangePassword', component: () => import('../views/ChangePasswordView.vue') },
  { path: '/account/profile', name: 'Profile', component: () => import('../views/ProfileView.vue') },
  { path: '/student/history', name: 'StudentHistory', component: () => import('../views/student/HistoryView.vue'), meta: { role: 'student' } },
  { path: '/student/history/:kind/:id', name: 'StudentHistoryDetail', component: () => import('../views/student/HistoryView.vue'), meta: { role: 'student' } },
  { path: '/admin/accounts', name: 'AdminAccounts', component: () => import('../views/admin/AccountOperationsView.vue'), meta: { role: 'admin' } },
  { path: '/admin/batches/:id', name: 'AdminBatch', component: () => import('../views/admin/BatchDetailView.vue'), meta: { role: 'admin' } },
  { path: '/admin/account-security', name: 'AdminAccountSecurity', component: () => import('../views/admin/AccountSecurityView.vue'), meta: { role: 'admin' } },
  { path: '/welcome', name: 'Welcome', component: () => import('../views/WelcomeView.vue'), meta: { guest: true } },
  { path: '/login', name: 'Login', component: () => import('../views/LoginView.vue'), meta: { guest: true } },

  { path: '/student/materials', name: 'StudentCourseMaterialsView', component: () => import('../views/shared/CourseMaterialsView.vue'), meta: { role: 'student' } },
  { path: '/student/offerings', name: 'StudentOfferingsView', component: () => import('../views/shared/OfferingsView.vue'), meta: { role: 'student' } },
  { path: '/teacher/materials', name: 'TeacherCourseMaterialsView', component: () => import('../views/shared/CourseMaterialsView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/offerings', name: 'TeacherOfferingsView', component: () => import('../views/shared/OfferingsView.vue'), meta: { role: 'teacher' } },
  { path: '/admin/materials', name: 'AdminCourseMaterialsView', component: () => import('../views/shared/CourseMaterialsView.vue'), meta: { role: 'admin' } },
  { path: '/admin/offerings', name: 'AdminOfferingsView', component: () => import('../views/shared/OfferingsView.vue'), meta: { role: 'admin' } },

  { path: '/student/gradebook', name: 'StudentGradebook', component: () => import('../views/GradebookView.vue'), meta: { role: 'student' } },
  { path: '/teacher/gradebook', name: 'TeacherGradebook', component: () => import('../views/GradebookView.vue'), meta: { role: 'teacher' } },
  { path: '/admin/gradebook', name: 'AdminGradebook', component: () => import('../views/GradebookView.vue'), meta: { role: 'admin' } },
  // Student
  { path: '/student', name: 'StudentHome', component: () => import('../views/student/DashboardView.vue'), meta: { role: 'student' } },
  { path: '/student/courses', name: 'StudentCourses', component: () => import('../views/student/CourseListView.vue'), meta: { role: 'student' } },
  { path: '/student/courses/:id', name: 'StudentCourseDetail', component: () => import('../views/student/CourseDetailView.vue'), meta: { role: 'student' } },
  { path: '/student/courses/:id/lessons/:lid', name: 'StudentLesson', component: () => import('../views/student/LessonView.vue'), meta: { role: 'student' } },
  { path: '/student/assignments', name: 'StudentAssignments', component: () => import('../views/student/AssignmentListView.vue'), meta: { role: 'student' } },
  { path: '/student/feedback', name: 'StudentFeedback', component: () => import('../views/student/FeedbackListView.vue'), meta: { role: 'student' } },
  { path: '/student/assignments/:id', name: 'StudentAssignmentDetail', component: () => import('../views/student/AssignmentDetailView.vue'), meta: { role: 'student' } },
  { path: '/student/submissions/:id', name: 'StudentSubmission', component: () => import('../views/student/SubmissionView.vue'), meta: { role: 'student' } },
  { path: '/student/exams', name: 'StudentExams', component: () => import('../views/student/ExamListView.vue'), meta: { role: 'student' } },
  { path: '/student/exams/:id', name: 'StudentExam', component: () => import('../views/student/ExamView.vue'), meta: { role: 'student' } },
  { path: '/student/experiments', name: 'StudentExperiments', component: () => import('../views/student/ExperimentView.vue'), meta: { role: 'student' } },
  { path: '/student/experiments/:id', name: 'StudentExperimentDetail', component: () => import('../views/student/ExperimentDetailView.vue'), meta: { role: 'student' } },
  { path: '/student/courses/:id/notebook/:lid', name: 'StudentNotebook', component: () => import('../views/student/NotebookView.vue'), meta: { role: 'student' } },
  { path: '/student/notifications', name: 'StudentNotifications', component: () => import('../views/student/StudentNotificationsView.vue'), meta: { role: 'student' } },
  // 自主练习（批次 5）：中心 / 我的练习 / 作答 / 作答反馈
  { path: '/student/practice', name: 'StudentPractice', component: () => import('../views/student/PracticeCenterView.vue'), meta: { role: 'student' } },
  { path: '/student/practice/mine', name: 'StudentPracticeMine', component: () => import('../views/student/PracticeMineView.vue'), meta: { role: 'student' } },
  { path: '/student/practice/problems/:id', name: 'StudentPracticeProblem', component: () => import('../views/student/PracticeProblemView.vue'), meta: { role: 'student' } },
  { path: '/student/practice/submissions/:id', name: 'StudentPracticeSubmission', component: () => import('../views/student/PracticeSubmissionView.vue'), meta: { role: 'student' } },

  // Teacher
  { path: '/teacher', name: 'TeacherHome', component: () => import('../views/teacher/DashboardView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/courses', name: 'TeacherCourses', component: () => import('../views/teacher/CourseManageView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/courses/:courseId/manage', name: 'TeacherChapterManage', component: () => import('../views/teacher/ChapterManageView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/assignments', name: 'TeacherAssignments', component: () => import('../views/teacher/AssignmentManageView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/assignments/:id/edit', name: 'TeacherAssignmentQuestionEdit', component: () => import('../views/teacher/QuestionEditView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/exams', name: 'TeacherExams', component: () => import('../views/teacher/ExamManageView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/exams/:id/grades', name: 'TeacherGrades', component: () => import('../views/teacher/GradesView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/exams/:id/grades/:submissionId', name: 'TeacherGradeDetail', component: () => import('../views/teacher/GradeDetailView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/exams/:id/proctor', name: 'TeacherExamProctor', component: () => import('../views/teacher/ProctorView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/exams/:id/edit', name: 'TeacherExamQuestionEdit', component: () => import('../views/teacher/ExamQuestionEditView.vue'), meta: { role: 'teacher' } },
 { path: '/teacher/experiments', name: 'TeacherExperiments', component: () => import('../views/teacher/ExperimentManageView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/experiments/create', redirect: '/teacher/experiments' },
  { path: '/teacher/experiments/:id/edit', redirect: '/teacher/experiments' },
  { path: '/teacher/experiments/:id/studio/:lid', name: 'TeacherExperimentModuleStudio', component: () => import('../views/teacher/ExperimentModuleStudioView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/submissions', name: 'TeacherExperimentSubmissions', component: () => import('../views/teacher/ExperimentSubmissionsView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/submissions/unified', name: 'TeacherUnifiedSubmissions', component: () => import('../views/teacher/UnifiedSubmissionsView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/submissions/:id', name: 'TeacherExperimentSubmissionDetail', component: () => import('../views/teacher/ExperimentSubmissionDetailView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/judge-submissions/:id', name: 'TeacherJudgeSubmissionDetail', component: () => import('../views/teacher/JudgeSubmissionDetailView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/classes', name: 'TeacherClasses', component: () => import('../views/teacher/ClassRosterView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/grades', name: 'TeacherGradeStatistics', component: () => import('../views/teacher/GradeStatisticsView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/environments', name: 'TeacherEnvironments', component: () => import('../views/teacher/EnvironmentView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/settings', name: 'TeacherSettings', component: () => import('../views/teacher/SettingsView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/notifications', name: 'TeacherNotifications', component: () => import('../views/teacher/NotificationsView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/ai-grading', name: 'TeacherAIGrading', component: () => import('../views/teacher/AIGradingReviewView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/ai-grading/:id', name: 'TeacherAIGradingDetail', component: () => import('../views/teacher/AIGradingReviewDetailView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/practice/leaderboard', name: 'TeacherPracticeLeaderboard', component: () => import('../views/teacher/PracticeLeaderboardView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/courses/:courseId/lessons/:lessonId/edit', name: 'TeacherLessonEdit', component: () => import('../views/teacher/LessonEditView.vue'), meta: { role: 'teacher' } },
  { path: '/teacher/courses/:id/studio/:lid', name: 'TeacherStudio', component: () => import('../views/teacher/StudioView.vue'), meta: { role: 'teacher' } },

  { path: '/admin/assignments/:id/edit', name: 'AdminAssignmentEdit', component: () => import('../views/teacher/QuestionEditView.vue'), meta: { role: 'admin' } },
  { path: '/admin/exams/:id/edit', name: 'AdminExamEdit', component: () => import('../views/teacher/ExamQuestionEditView.vue'), meta: { role: 'admin' } },
  // 管理员复用教师考试页面：管理列表、成绩总览与逐份详情都走同一组件与同一 API 授权
  { path: '/admin/exams', name: 'AdminExams', component: () => import('../views/teacher/ExamManageView.vue'), meta: { role: 'admin' } },
  { path: '/admin/exams/:id/grades', name: 'AdminGrades', component: () => import('../views/teacher/GradesView.vue'), meta: { role: 'admin' } },
  { path: '/admin/exams/:id/grades/:submissionId', name: 'AdminGradeDetail', component: () => import('../views/teacher/GradeDetailView.vue'), meta: { role: 'admin' } },
  // Admin
  { path: '/admin', name: 'AdminHome', component: () => import('../views/admin/DashboardView.vue'), meta: { role: 'admin' } },
  { path: '/admin/users', name: 'AdminUsers', component: () => import('../views/admin/UserListView.vue'), meta: { role: 'admin' } },
  { path: '/admin/users/:id/edit', name: 'AdminUserEdit', component: () => import('../views/admin/UserEditView.vue'), meta: { role: 'admin' } },
  { path: '/admin/academics', name: 'AdminAcademics', component: () => import('../views/admin/AcademicManageView.vue'), meta: { role: 'admin' } },
  { path: '/admin/courses', name: 'AdminCourses', component: () => import('../views/teacher/CourseManageView.vue'), meta: { role: 'admin' } },
  { path: '/admin/courses/:courseId/manage', name: 'AdminCourseManage', component: () => import('../views/teacher/ChapterManageView.vue'), meta: { role: 'admin' } },
  { path: '/admin/courses/:courseId/lessons/:lessonId/edit', name: 'AdminLessonEdit', component: () => import('../views/teacher/LessonEditView.vue'), meta: { role: 'admin' } },
  { path: '/admin/courses/:id/studio/:lid', name: 'AdminCourseStudio', component: () => import('../views/teacher/StudioView.vue'), meta: { role: 'admin' } },
  { path: '/admin/experiments', name: 'AdminExperiments', component: () => import('../views/admin/ExperimentManageView.vue'), meta: { role: 'admin' } },
  { path: '/admin/environments', name: 'AdminEnvironments', component: () => import('../views/admin/EnvironmentManageView.vue'), meta: { role: 'admin' } },
  { path: '/admin/submissions', name: 'AdminExperimentSubmissions', component: () => import('../views/teacher/ExperimentSubmissionsView.vue'), meta: { role: 'admin' } },
  { path: '/admin/submissions/unified', name: 'AdminUnifiedSubmissions', component: () => import('../views/teacher/UnifiedSubmissionsView.vue'), meta: { role: 'admin' } },
  { path: '/admin/submissions/:id', name: 'AdminExperimentSubmissionDetail', component: () => import('../views/teacher/ExperimentSubmissionDetailView.vue'), meta: { role: 'admin' } },
  { path: '/admin/judge-submissions/:id', name: 'AdminJudgeSubmissionDetail', component: () => import('../views/teacher/JudgeSubmissionDetailView.vue'), meta: { role: 'admin' } },
  { path: '/admin/ai-grading', name: 'AdminAIGrading', component: () => import('../views/teacher/AIGradingReviewView.vue'), meta: { role: 'admin' } },
  { path: '/admin/ai-grading/:id', name: 'AdminAIGradingDetail', component: () => import('../views/teacher/AIGradingReviewDetailView.vue'), meta: { role: 'admin' } },
  // 自主练习管理员面：题库 / 编辑 / 设置 / 用量 / 复核
  { path: '/admin/practice/problems', name: 'AdminPracticeProblems', component: () => import('../views/admin/PracticeProblemsView.vue'), meta: { role: 'admin' } },
  { path: '/admin/practice/problems/new', name: 'AdminPracticeProblemNew', component: () => import('../views/admin/PracticeProblemEditView.vue'), meta: { role: 'admin' } },
  { path: '/admin/practice/problems/:id/edit', name: 'AdminPracticeProblemEdit', component: () => import('../views/admin/PracticeProblemEditView.vue'), meta: { role: 'admin' } },
  { path: '/admin/practice/settings', name: 'AdminPracticeSettings', component: () => import('../views/admin/PracticeSettingsView.vue'), meta: { role: 'admin' } },
  { path: '/admin/practice/usage', name: 'AdminPracticeUsage', component: () => import('../views/admin/PracticeUsageView.vue'), meta: { role: 'admin' } },
  { path: '/admin/practice/appeals', name: 'AdminPracticeAppeals', component: () => import('../views/admin/PracticeAppealsView.vue'), meta: { role: 'admin' } },
  { path: '/admin/logs', name: 'AdminLogs', component: () => import('../views/admin/AdminLogsView.vue'), meta: { role: 'admin' } },

  { path: '/', redirect: '/welcome' },
  { path: '/:pathMatch(.*)*', redirect: '/welcome' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

let fetchMePromise = null

router.beforeEach(async (to, from, next) => {
  const auth = useAuthStore()

  // Guest-only pages (login/welcome)
  if (to.meta.guest) {
    if (auth.isAuthenticated) return next(auth.user?.must_change_password ? '/account/password' : homeForRole(auth.role))
    return next()
  }

  // Protected pages — 先尝试 cookie 恢复 session
  if (!auth.isAuthenticated) {
    const restored = await auth.tryRestoreSession()
    if (!restored) {
      return next('/login')
    }
  }

  // Restore user if page reload (deduplicate)
  if (!auth.user) {
    if (!fetchMePromise) {
      fetchMePromise = auth.fetchMe().finally(() => {
        fetchMePromise = null
      })
    }
    const u = await fetchMePromise
    if (!u) return next('/login')
  }

  if (auth.user?.must_change_password && to.path !== '/account/password') return next('/account/password')
  if (!auth.user?.must_change_password && to.path === '/account/password') return next('/account/profile')

  // Role check
  if (to.meta.role && to.meta.role !== auth.role) {
    return next(homeForRole(auth.role))
  }

  next()
})

export default router
