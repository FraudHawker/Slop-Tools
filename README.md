# Slop Tools

Open source OSINT utilities. Each tool is self-contained — pick the one you need, `cd` into it, `docker compose up -d`.

## Tools

| Tool | Description | Status |
|------|-------------|--------|
| [metadata-tool](metadata-tool/) | Bulk metadata extraction, PII detection, GPS mapping, metadata stripping and randomization | Ready |

## How to use

Each tool has its own directory with a Dockerfile, docker-compose.yml, and README. No shared dependencies between tools.

This command clones the full `Slop-Tools` repository:

```bash
git clone https://github.com/FraudHawker/Slop-Tools.git
```

Then `cd` into the tool you want. For example:

```bash
cd Slop-Tools/metadata-tool
cp .env.example .env
docker compose up -d --build
```

Each tool README also includes a "Download Just This Tool" option if you want a sparse checkout instead of the full repo.

## License

MIT
