FROM python:3.14-slim

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src
COPY services/registry /app/services/registry

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

CMD ["python", "services/registry/main.py"]
