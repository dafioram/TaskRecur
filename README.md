1. Copy `.env.example` to `.env` and adjust
2. Run `docker compose up -d --build`
3. Open http://YOUR-SERVER-IP:5000

Docker uses Waitress to read client requests before dispatching them to a
single application thread. An idle connection can stay open while other
requests run. Rebuild the image to pick up the server change. Settings in
`GUNICORN_CMD_ARGS` apply only to deployments still using Gunicorn.

## Backups
Backups are on demand. Each call writes a snapshot to `data/backups/` and keeps the 5 newest:

    curl -X POST -H "X-Backup-Key: $API_BACKUP_KEY" http://YOUR-SERVER-IP:5000/api/backup

Take one before upgrading the app.

## Restore
1. Stop the app (`docker compose down`)
2. Delete `data/tasks.db*` (the `.db`, `-wal` and `-shm` files)
3. Copy the chosen `data/backups/tasks_YYYYMMDD_HHMMSS.db` to `data/tasks.db`
4. Start the app (`docker compose up -d`)
