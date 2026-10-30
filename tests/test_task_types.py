from datetime import datetime, timezone

from agent_test.types.task import Task, TaskStatus


def test_task_status_is_a_type_alias_not_a_dangling_annotation():
    # TaskStatus 必须是可绑定到模型字段的类型别名，而不是无效的裸注解。
    assert TaskStatus is not None
    task = Task(
        task_id="t1",
        session_id="s1",
        parent_task_id=None,
        title="demo",
        objective="verify task model",
        status="PENDING",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata={},
    )
    assert task.status == "PENDING"
