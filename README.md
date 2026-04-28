# Slop Tools

Open source OSINT utilities. Each tool is self-contained — pick the one you need, `cd` into it, `docker compose up -d`.

## Tools

| Tool | Description | Status |
|------|-------------|--------|
| [metadata-tool](metadata-tool/) | Bulk metadata extraction, PII detection, GPS mapping, metadata stripping and randomization | Ready |

## How to use

Each tool has its own directory with a Dockerfile, docker-compose.yml, and README. No shared dependencies between tools.

```bash
git clone https://github.com/FraudHawker/Slop-Tools.git
cd Slop-Tools/metadata-tool
docker compose up -d
```

## License

MIT
