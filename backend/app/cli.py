import argparse

from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import User
from app.security import hash_password, validate_password_rules


def create_admin(username: str, password: str, real_name: str):
    try:
        validate_password_rules(password, username)
    except ValueError as exc:
        raise SystemExit(f"密码不符合要求：{exc}") from exc
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        existing = db.scalar(select(User).where(User.username == username).with_for_update())
        if existing:
            from app.services.account_service import change_credentials, audit
            change_credentials(db, existing, hash_password(password))
            audit(db, existing.id, 'password.cli_reset', 'user', existing.id)
            existing.real_name = real_name
            existing.role = "admin"
            existing.status = "active"
            db.commit()
            print(f"Updated admin user: {username}")
            return
        user = User(
            username=username,
            real_name=real_name,
            role="admin",
            status="active",
            password_hash=hash_password(password),
        )
        db.add(user)
        db.commit()
        print(f"Created admin user: {username}")


def seed_environments(enqueue: bool):
    """初始化环境档位种子（幂等）——数据全部来自内置常量，无任意输入面。

    --enqueue 时同时把 draft/failed 版本入队构建（Redis 只负责唤醒，DB 是事实源）。
    """
    from app.config import get_settings
    from app.services.environment_seed import seed_environment_catalog

    settings = get_settings()
    redis_client = None
    if enqueue:
        import redis as _redis

        redis_client = _redis.Redis.from_url(settings.redis_url, decode_responses=True)
    with SessionLocal() as db:
        result = seed_environment_catalog(db, settings, enqueue=enqueue, redis_client=redis_client)
        print(f"Profiles created: {result.profiles_created}")
        print(f"Packages created: {result.packages_created}")
        print(f"Versions created: {result.versions_created}")
        print(f"Enqueued: {result.enqueued}")
        print(f"Already available: {result.already_available}")
        print(f"Skipped: {result.skipped}")


def seed_demo(reset: bool, reference_date: str | None, skip_env_check: bool, force_fixture: bool):
    """播种 Demo 演示数据（评审 3.3）：幂等、固定参考日期、仅清理 Demo 自有数据。"""
    from app.seed_demo import run_demo_seed

    with SessionLocal() as db:
        summary = run_demo_seed(
            db,
            reference_date=reference_date,
            reset=reset,
            skip_env_check=skip_env_check,
            force_fixture=force_fixture,
        )
        print("=" * 64)
        print("人工智能基础实验平台 —— Demo 演示数据播种完成")
        print("=" * 64)
        for key, value in summary.items():
            print(f"{key:24s}: {value}")
        print("\n固定演示账号（默认密码 Demo1234!，DAI_DEMO_PASSWORD 可覆盖）：")
        print("  管理端: demo_admin")
        print("  教师端: teacher_zhang / teacher_chen / teacher_zhao")
        print("  学生端: demo_student_elite / demo_student_average /")
        print("          demo_student_struggling / demo_student_new")
        print("  背景学生: student_24621601_01 .. student_24621606_10")


def main():
    parser = argparse.ArgumentParser(description="DAI backend management commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("audit-student-identities", help="只读列出缺失学号或登录名与学号不一致的旧学生")
    admin_parser = subparsers.add_parser("create-admin")
    admin_parser.add_argument("--username", required=True)
    admin_parser.add_argument("--password", required=True)
    admin_parser.add_argument("--real-name", default="Administrator")
    seed_parser = subparsers.add_parser("seed-environments", help="初始化环境档位种子（幂等）")
    seed_parser.add_argument("--enqueue", action="store_true", help="同时入队构建任务")
    demo_parser = subparsers.add_parser("seed-demo", help="播种 Demo 演示数据（幂等，仅清理 Demo 自有数据）")
    demo_parser.add_argument("--reset-demo", action="store_true",
                             help="先按所有权登记表清理既有 Demo 数据再播种")
    demo_parser.add_argument("--reference-date",
                             help="参考日期：now|today 取运行当日，YYYY-MM-DD 钉死；缺省用固定默认（2026-12-07）")
    demo_parser.add_argument("--skip-env-check", action="store_true",
                             help="跳过 basic 环境版本前置校验（仅供测试）")
    demo_parser.add_argument("--force-fixture", action="store_true",
                             help="强制全部提交使用 seed_fixture（不做真实 Docker 判题）")
    args = parser.parse_args()
    if args.command == 'audit-student-identities':
        import json
        from sqlalchemy import or_
        with SessionLocal() as db:
            rows = db.scalars(select(User).where(User.role == 'student', or_(
                User.student_no.is_(None), User.student_no == '', User.username != User.student_no))).all()
            print(json.dumps([{'id': row.id, 'username': row.username, 'student_no': row.student_no}
                              for row in rows], ensure_ascii=False))
    elif args.command == "create-admin":
        create_admin(args.username, args.password, args.real_name)
    elif args.command == "seed-environments":
        seed_environments(args.enqueue)
    elif args.command == "seed-demo":
        seed_demo(args.reset_demo, args.reference_date, args.skip_env_check, args.force_fixture)


if __name__ == "__main__":
    main()
