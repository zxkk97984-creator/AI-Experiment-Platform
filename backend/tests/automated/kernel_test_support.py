"""Test helpers for the async kernel execution contract.

单元格执行改为异步后（202 + execution_id），既有用例需要「跑完一次在途执行」的等价语义。
测试进程没有 kernel worker，这里直接调用真实 worker 的 ``process_execution``，
只把 Docker 边界替换成 fake kernel manager（不建容器、不碰 Redis session）。
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def kernel_runtime(fake_manager):
    """替换 kernel 执行边界：fake manager + 不建容器的工作区准备。"""
    with patch("app.services.kernel_manager.get_kernel_manager", lambda: fake_manager), \
         patch("app.services.lab_workspace.ensure_record_kernel", lambda *args, **kwargs: None):
        yield fake_manager


def drain_execution(session_factory, settings, execution_id):
    """认领并同步执行一条 queued 执行（复用真实任务状态机与终态落库）。"""
    from app.services.kernel_queue import claim_execution
    from app.worker import kernel_worker

    with session_factory() as db:
        claimed = claim_execution(db, settings)
    assert claimed is not None, "没有可认领的执行任务"
    claimed_id, token = claimed
    assert claimed_id == execution_id, f"认领到 {claimed_id} 而不是 {execution_id}"
    kernel_worker.process_execution(session_factory, claimed_id, token, settings)
    return claimed_id


def post_execute(client, record_id, cell_id, code, headers, *, client_request_id=None):
    """POST 一次执行，返回 202 响应 JSON（不等待完成）。"""
    payload = {"code": code}
    if client_request_id is not None:
        payload["client_request_id"] = client_request_id
    response = client.post(
        f"/api/v1/experiments/records/{record_id}/cells/{cell_id}/execute",
        headers=headers, json=payload,
    )
    assert response.status_code == 202, response.text
    return response.json()


def run_cell(client, session_factory, settings, fake_manager, record_id, cell_id, code, headers):
    """POST + 同步跑完一次执行，返回 execution_id。"""
    accepted = post_execute(client, record_id, cell_id, code, headers)
    with kernel_runtime(fake_manager):
        drain_execution(session_factory, settings, accepted["execution_id"])
    return accepted["execution_id"]
