from fastapi import APIRouter

from .account_management import router as account_management_router
from .admin_logs import router as admin_logs_router
from .ai_grading import router as ai_grading_router
from .academics import router as academics_router
from .announcements import router as announcements_router
from .assignments import router as assignments_router
from .dashboard import router as dashboard_router
from .auth import router as auth_router
from .courses import router as courses_router
from .course_covers import router as course_covers_router
from .environments import router as environments_router
from .exams import router as exams_router
from .experiments import router as experiments_router
from .judge import router as judge_router
from .jupyter import router as jupyter_router
from .lesson_progress import router as lesson_progress_router
from .lesson_videos import router as lesson_videos_router
from .notebooks import router as notebooks_router
from .studio import router as studio_router
from .statistics import router as statistics_router
from .search import router as search_router
from .submissions import router as submissions_router
from .notifications import router as notifications_router
from .student_history import router as student_history_router
from .users import router as users_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(academics_router)
api_router.include_router(announcements_router)
api_router.include_router(student_history_router)
api_router.include_router(users_router)
api_router.include_router(courses_router)
api_router.include_router(course_covers_router)
api_router.include_router(environments_router)
api_router.include_router(assignments_router)
api_router.include_router(ai_grading_router)
api_router.include_router(dashboard_router)
api_router.include_router(judge_router)
api_router.include_router(lesson_progress_router)
api_router.include_router(lesson_videos_router)
api_router.include_router(exams_router)
api_router.include_router(experiments_router)
api_router.include_router(jupyter_router)
api_router.include_router(notebooks_router)
api_router.include_router(studio_router)
api_router.include_router(statistics_router)
api_router.include_router(search_router)
api_router.include_router(submissions_router)
api_router.include_router(notifications_router)
api_router.include_router(admin_logs_router)

api_router.include_router(account_management_router)
