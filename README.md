# Slop Tools

Open source OSINT and production utilities. Each tool is self-contained and Docker-first: clone the repo, `cd` into the tool you want, and run `docker compose up -d --build`.

## Tools

| Tool | Description | Status |
|------|-------------|--------|
| [metadata-tool](metadata-tool/) | Bulk metadata extraction, PII detection, GPS mapping, metadata stripping and randomization | Ready |
| [image-verify](image-verify/) | Image provenance and manipulation triage — C2PA, thumbnail mismatch, metadata integrity, forensic heuristics | Ready |
| [yt-subtitler](yt-subtitler/) | Clip YouTube segments, transcribe or translate subtitles, and burn them into the output video | Ready |

## How to use

Each tool has its own directory with a Dockerfile, `docker-compose.yml`, and README. No shared dependencies between tools, and the public setup path is Docker-only.

This command clones the full `Slop-Tools` repository:

```bash
git clone https://github.com/FraudHawker/Slop-Tools.git
```

Then `cd` into the tool you want. Current examples:

```bash
cd Slop-Tools/metadata-tool
cp .env.example .env
docker compose up -d --build
```

```bash
cd Slop-Tools/image-verify
cp .env.example .env
docker compose up -d --build
```

```bash
cd Slop-Tools/yt-subtitler
docker compose up -d --build
```

Each tool README also includes a "Download Just This Tool" option if you want a sparse checkout instead of the full repo.

## License

MIT
