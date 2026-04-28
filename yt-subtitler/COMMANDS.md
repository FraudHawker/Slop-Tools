# Commands

## Start
```bash
docker compose up -d --build
```
App runs at http://localhost:8077

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

## Configure LLM endpoint
Open the **Settings** tab in the web UI, or set env vars in `docker-compose.yml`:
```yaml
LLM_BASE_URL: http://host.docker.internal:1234/v1
LLM_MODEL: ""
LLM_API_KEY: lm-studio
```

## Check what models are loaded
```bash
curl http://localhost:1234/v1/models
```

## Clear all clips and data
```bash
docker compose down
rm -rf data/
docker compose up -d
```

## Keep whisper model cache across rebuilds
The `./models/` volume persists whisper weights (~3GB for large-v3). Don't delete it unless you want to re-download.

## Run without Docker
```bash
pip install -r requirements.txt
python app.py
```
Requires ffmpeg with libass and deno installed.

## Run smoke test
```bash
./test.sh
```
