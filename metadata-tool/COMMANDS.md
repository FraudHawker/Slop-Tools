# Commands

## Start
```bash
docker compose up -d
```
App runs at http://localhost:8080

## Stop
```bash
docker compose down
```

## Rebuild (after code changes)
```bash
docker compose up -d --build
```

## View logs
```bash
docker compose logs -f
```

## Configure (.env)
Create `.env` from the example:
```bash
cp .env.example .env
```

`.env` is a hidden file (dot-prefix). To edit it:
```bash
nano .env
```
Or to see it in Finder, press `Cmd+Shift+.` to toggle hidden files.

Example — change the port:
```
PORT=9090
```
Then restart.

## Reset everything (wipe all data)
```bash
docker compose down
rm -rf data/
docker compose up -d
```

## Export data while running
- CSV: http://localhost:8080/export/csv
- JSON: http://localhost:8080/export/json
