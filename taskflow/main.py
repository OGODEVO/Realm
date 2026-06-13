"""
TaskFlow — A FastAPI task management API.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# App & in-memory store
# ---------------------------------------------------------------------------

app = FastAPI(
    title="TaskFlow",
    description="A simple task management API built with FastAPI.",
    version="1.0.0",
)

_db: dict[int, dict] = {}
_counter: int = 0

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TaskIn(BaseModel):
    """Schema for creating a new task."""

    title: str = Field(
        ..., min_length=1, max_length=200, description="Task title"
    )
    description: Optional[str] = Field(
        None, max_length=2000, description="Optional task description"
    )
    completed: bool = Field(False, description="Whether the task is done")


class TaskOut(BaseModel):
    """Schema returned to the client."""

    id: int = Field(..., description="Unique task identifier")
    title: str = Field(..., description="Task title")
    description: Optional[str] = Field(None, description="Task description")
    completed: bool = Field(False, description="Whether the task is done")
    created_at: datetime = Field(..., description="Timestamp of creation")
    updated_at: datetime = Field(..., description="Timestamp of last update")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post(
    "/tasks",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new task",
)
def create_task(payload: TaskIn):
    """Create a task and return it with an auto-generated id and timestamps."""
    global _counter
    _counter += 1
    now = datetime.now(timezone.utc)
    record: dict = {
        "id": _counter,
        "title": payload.title,
        "description": payload.description,
        "completed": payload.completed,
        "created_at": now,
        "updated_at": now,
    }
    _db[_counter] = record
    return record


@app.get(
    "/tasks",
    response_model=list[TaskOut],
    summary="List all tasks",
)
def list_tasks(completed: Optional[bool] = None):
    """
    Return every task, optionally filtered by their ``completed`` status.
    """
    tasks = list(_db.values())
    if completed is not None:
        tasks = [t for t in tasks if t["completed"] == completed]
    return tasks


@app.get(
    "/tasks/{task_id}",
    response_model=TaskOut,
    summary="Get a single task by id",
)
def get_task(task_id: int):
    """Return the task with the given ``task_id`` or 404."""
    record = _db.get(task_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )
    return record


@app.put(
    "/tasks/{task_id}",
    response_model=TaskOut,
    summary="Update an existing task",
)
def update_task(task_id: int, payload: TaskIn):
    """Replace a task's fields.  Raises 404 if the task does not exist."""
    record = _db.get(task_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )
    now = datetime.now(timezone.utc)
    record["title"] = payload.title
    record["description"] = payload.description
    record["completed"] = payload.completed
    record["updated_at"] = now
    _db[task_id] = record
    return record


@app.delete(
    "/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a task",
)
def delete_task(task_id: int):
    """Remove a task from the store.  Returns 204 on success, 404 if missing."""
    if task_id not in _db:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )
    del _db[task_id]
    return None


# ---------------------------------------------------------------------------
# Health-check (optional, handy)
# ---------------------------------------------------------------------------


@app.get("/health", summary="Health check")
def health():
    return {"status": "ok", "task_count": len(_db)}
