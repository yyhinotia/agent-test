from datetime import datetime
from typing import Literal
from pydantic import BaseModel

TaskStatus: Literal['PENDING', 'RUNNING','PAUSED', 'COMPLETED', 'FAILED', 'CANCELLED']

class Task(BaseModel):
    task_id: str
    session_id: str
    parent_task_id: str | None

    title: str
    objective: str

    status: TaskStatus = 'PENDING'

    created_at: datetime
    updated_at: datetime

    metadata: dict