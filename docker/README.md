# docker/ (moved)

Compose file now lives at **[`boot/docker-compose.yml`](../boot/docker-compose.yml)**.

```bash
docker compose -f boot/docker-compose.yml up -d
```

`docker/docker-compose.yml` is a thin Compose `include` redirect for old scripts.
