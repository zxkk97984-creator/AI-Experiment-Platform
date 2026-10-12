"""Match reset spreadsheets without creating users or changing credentials."""
from collections import defaultdict
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from app.models import AdminBatch, AdminBatchItem, ExamSubmission, User
from app.services import account_service as accounts
from app.services.time_utils import utc_now


def create_preview(db, actor, inputs, settings):
    numbers = {data.get('student_no') for _, data in inputs if data.get('student_no')}
    users = defaultdict(list)
    for user in db.scalars(select(User).where(User.student_no.in_(numbers)).order_by(User.id)):
        users[user.student_no].append(user)
    exam_students = set(db.scalars(select(ExamSubmission.student_id)
                                  .where(ExamSubmission.status == 'started')))
    _, _, version = accounts.temporary_policy(db, settings)
    batch = AdminBatch(id=str(uuid4()), actor_id=actor.id, kind='password_reset',
                       scope={'selection': 'excel'}, summary={}, preview_digest=str(uuid4()),
                       policy_version=version, expires_at=utc_now() + timedelta(hours=24))
    db.add(batch)
    db.flush()
    seen = set()
    eligible = skipped = errors_count = 0
    for number, data in inputs:
        errors = list(data.get('_parse_errors', []))
        student_no = data.get('student_no', '')
        matches = users.get(student_no, [])
        user = matches[0] if len(matches) == 1 else None
        if not student_no:
            errors.append('学号不能为空')
        elif not matches:
            errors.append('学号不存在，请先核对学生账号')
        elif len(matches) != 1:
            errors.append('学号对应多个账号，请先修正账号数据')
        elif user.role != 'student':
            errors.append('学号对应的账号不是学生')
        elif data.get('real_name') and data['real_name'] != user.real_name:
            errors.append('姓名与学生账号不一致')
        row_data = {'student_no': student_no, 'real_name': data.get('real_name', '')}
        if errors:
            errors_count += 1
            db.add(AdminBatchItem(batch_id=batch.id, row_number=number, data=row_data,
                                 result={'errors': errors}, status='pending'))
            continue
        row_data['real_name'] = user.real_name
        if user.id in seen:
            skipped += 1
            db.add(AdminBatchItem(batch_id=batch.id, row_number=number, user_id=user.id,
                                 data=row_data, result={'duplicate': True, 'message': '重复学号，跳过；同一学生只重置一次'},
                                 status='skipped'))
            continue
        seen.add(user.id)
        message = ('账号已禁用，跳过' if user.status != 'active' else
                   '学生正在考试，跳过' if user.id in exam_students else None)
        skipped += bool(message)
        eligible += not bool(message)
        db.add(AdminBatchItem(batch_id=batch.id, row_number=number, user_id=user.id,
                             expected_version=user.credential_version, data=row_data,
                             result={'message': message or '重置后旧登录失效，必须改密'},
                             status='skipped' if message else 'pending'))
    batch.summary = {'selected': len(inputs), 'eligible': eligible, 'skipped': skipped, 'errors': errors_count}
    db.commit()
    return batch
