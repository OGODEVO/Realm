# TaskFlow

A lightweight task management API built with **FastAPI** and Python 3.11+.

## Quick start

```bash
cd taskflow

# Create & activate a virtualenv (optional but recommended)
python3 -m venv .venv && source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the dev server
uvicorn main:app --reload --port 8000
```

Open http://127.0.0.1:8000/docs for the interactive Swagger UI.

## Endpoints

| Method | Path            | Description                |
|--------|-----------------|----------------------------|
| POST   | `/tasks`        | Create a new task          |
| GET    | `/tasks`        | List all tasks             |
| GET    | `/tasks/{id}`   | Get a single task by ID    |
| PUT    | `/tasks/{id}`   | Update an existing task    |
| DELETE | `/tasks/{id}`   | Delete a task              |
| GET    | `/health`       | Health check               |

### Query parameters

- `GET /tasks?completed=true` — filter tasks by completion status.

### Example — create a task

```bash
curl -s -X POST http://127.0.0.1:8000/tasks \
  -H "Content-Type: application/json" \
  -d '{"title": "Buy groceries", "description": "Milk, eggs, bread"}' \
  | python3 -m json.tool
```

### Example — list tasks

```bash
curl -s http://127.0.0.1:8000/tasks | python3 -m json.tool
```

### Example — delete a task

```bash
curl -s -X DELETE http://127.0.0.1:8000/tasks/1
```

## Data model

| Field       | Type     | Notes                     |
|-------------|----------|---------------------------|
| `id`        | int      | Auto-generated            |
| `title`     | str      | Required, 1–200 chars     |
| `description` | str   | Optional, ≤2000 chars     |
| `completed` | bool     | Default `false`           |
| `created_at` | datetime | UTC, set on creation     |
| `updated_at` | datetime | UTC, updated on changes  |

Data is stored in memory (volatile). Restarting the server resets all tasks.

## License

MIT
