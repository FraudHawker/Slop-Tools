# Commands

## Start
```bash
docker compose up -d
```
App runs at http://localhost:8085

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

## Clear all analyses
- Via the web UI: click "Clear All" on the main page
- Via command line:
```bash
rm -rf data/
docker compose restart
```

## Export a result as JSON
```
http://localhost:8085/result/<analysis_id>/json
```
