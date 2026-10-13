"""考试编程题异步判题测试"""
import datetime
from datetime import timezone, timedelta
import json
from unittest.mock import patch
from conftest import auth_header, create_course_db, create_user, login
from teaching_test_support import grant_course_students
API = "/api/v1"

def _h(tok): return auth_header(tok)

def _setup(client, db_sf):
    create_user(db_sf, "ejt", "teacher"); create_user(db_sf, "ejs", "student")
    t_tok, _ = login(client, "ejt"); s_tok, _ = login(client, "ejs")
    cid = create_course_db(db_sf, teacher_username="ejt", title="EC", status="published", visibility="public")
    client.post(f"{API}/courses/{cid}/enroll", headers=_h(s_tok))
    now = datetime.datetime.now(timezone.utc)
    e = client.post(f"{API}/exams", headers=_h(t_tok), json={"course_id":cid,"title":"CE","duration_minutes":60,"start_at":(now-timedelta(hours=1)).isoformat(),"end_at":(now+timedelta(hours=1)).isoformat()})
    eid = e.json()["id"]
    grant_course_students(db_sf, cid, ["ejs"])
    q = client.post(f"{API}/exams/{eid}/questions", headers=_h(t_tok), json={"question_type":"code","prompt":"Q","points":30,"order_index":0,"hidden_tests":"def test():\n    assert add(1,2)==3","starter_code":"def add(a,b):\n    ","correct_answer":{},"grading_mode":"legacy"})
    assert q.status_code == 201
    from app.services import grading_version_service as versions
    from app.services.grading_values import encode_value

    with db_sf() as db:
        version = versions.create_draft(
            db, question_kind="exam", question_id=q.json()["id"],
            payload={"binary_cases": [
                {"id": "B1", "input": encode_value((1, 2)), "expected": encode_value(3),
                 "compare": {"kind": "literal"}},
                {"id": "B2", "input": encode_value((-1, 1)), "expected": encode_value(0),
                 "compare": {"kind": "literal"}},
            ]},
            entry_function="add", grading_mode_snapshot="legacy",
        )
        versions.confirm_version(db, version, confirm_isolated_case_v2=True)
        versions.activate_version(db, version)
        versions.set_question_current_version(db, question_kind="exam",
                                              question_id=q.json()["id"], version_id=version.id)
        db.commit()
    client.patch(
        f"{API}/exams/{eid}", headers=_h(t_tok),
        json={"status":"published", "show_score_after_grading": True},
    )
    return {"t_tok":t_tok,"s_tok":s_tok,"eid":eid,"qid":q.json()["id"]}

def _process_sync(answer_id, db_session_factory):
    from app.config import get_settings
    import fakeredis
    from app.worker.judge_worker import process_exam_answer
    with db_session_factory() as db:
        process_exam_answer(db, fakeredis.FakeStrictRedis(), get_settings(), answer_id)


def _runner_result(values):
    from app.services.grading_runner import RESULT_PREFIX
    from app.services.grading_values import encode_value

    frames = [
        {"id": case_id, "status": "ok", "value": encode_value(value)}
        for case_id, value in zip(("B1", "B2"), values, strict=True)
    ]
    return "", "\n".join(RESULT_PREFIX + json.dumps(frame) for frame in frames), 0, 150

def test_correct_scores_full(client, db_session_factory):
    ctx = _setup(client, db_session_factory)
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx["s_tok"]))
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['qid']}", headers=_h(ctx["s_tok"]), json={"expected_write_generation": 1, "code_answer":"def add(a,b):\n    return a+b"})
    client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx["s_tok"]), json={"expected_write_generation": 1})
    with db_session_factory() as db:
        from app.models import ExamAnswer
        ans = db.query(ExamAnswer).first()
        assert ans.grading_status == "queued"
    with patch("app.worker.judge_worker._run_docker_data_runner",
               return_value=_runner_result([3, 0])):
        _process_sync(ans.id, db_session_factory)
    g = client.get(f"{API}/exams/{ctx['eid']}/my-grade", headers=_h(ctx["s_tok"]))
    assert g.json()["score"] == 30
    assert g.json()["status"] == "graded"

def test_wrong_scores_zero(client, db_session_factory):
    ctx = _setup(client, db_session_factory)
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx["s_tok"]))
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['qid']}", headers=_h(ctx["s_tok"]), json={"expected_write_generation": 1, "code_answer":"def add(a,b):\n    return 0"})
    client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx["s_tok"]), json={"expected_write_generation": 1})
    with db_session_factory() as db:
        from app.models import ExamAnswer
        ans = db.query(ExamAnswer).first()
    with patch("app.worker.judge_worker._run_docker_data_runner",
               return_value=_runner_result([0, 0])):
        _process_sync(ans.id, db_session_factory)
    g = client.get(f"{API}/exams/{ctx['eid']}/my-grade", headers=_h(ctx["s_tok"]))
    assert g.json()["score"] == 0

def test_docker_fail_marked_system_error(client, db_session_factory):
    ctx = _setup(client, db_session_factory)
    client.post(f"{API}/exams/{ctx['eid']}/start", headers=_h(ctx["s_tok"]))
    client.put(f"{API}/exams/{ctx['eid']}/answers/{ctx['qid']}", headers=_h(ctx["s_tok"]), json={"expected_write_generation": 1, "code_answer":"def add(a,b):\n    return a+b"})
    client.post(f"{API}/exams/{ctx['eid']}/submit", headers=_h(ctx["s_tok"]), json={"expected_write_generation": 1})
    with db_session_factory() as db:
        from app.models import ExamAnswer
        ans = db.query(ExamAnswer).first()
    with patch("app.worker.judge_worker._run_docker_data_runner", side_effect=Exception("no docker")):
        _process_sync(ans.id, db_session_factory)
    g = client.get(f"{API}/exams/{ctx['eid']}/my-grade", headers=_h(ctx["s_tok"]))
    assert g.json()["answers"][0]["system_error"] is not None
