# image-verify

Image provenance and manipulation triage for investigators, journalists, and researchers.

Upload an image and review several signal types together:

- metadata integrity
- C2PA provenance, when present
- embedded thumbnail mismatch checks
- classic forensic heuristics like ELA, noise, and JPEG ghosts
- reverse image search handoff links

## What This Tool Is

This is a triage tool, not a magic detector. It helps surface useful signals and caveats in one place.

- A valid C2PA chain is strong evidence.
- A missing C2PA chain is normal and does not imply tampering.
- In the published Docker build, C2PA support is required rather than optional.
- Thumbnail mismatch can be meaningful, but it is not proof on its own.
- ELA, noise, and JPEG ghost analysis are supplementary heuristics with real limits against modern editing and generative tools.

## Quick Start

This command clones the full `Slop-Tools` repository, then opens the `image-verify` folder inside it.

```bash
git clone https://github.com/FraudHawker/Slop-Tools.git
cd Slop-Tools/image-verify
cp .env.example .env
docker compose up -d --build
```

Open `http://localhost:8085`

The published Docker image requires `c2pa-python`, so C2PA provenance checks are expected to be available inside the container.

### Download just this tool

If you only want the `image-verify` folder instead of the full repo:

```bash
git clone --filter=blob:none --sparse https://github.com/FraudHawker/Slop-Tools.git
cd Slop-Tools
git sparse-checkout set image-verify
cd image-verify
cp .env.example .env
docker compose up -d --build
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 8085 | Host port |
| `MAX_UPLOAD_MB` | 50 | Maximum upload size per image |
| `SECRET_KEY` | `change-me-in-production` | Flask session key |

## Output Levels

- `verified`: valid C2PA provenance chain
- `tampered`: cryptographic provenance was present but failed validation
- `suspicious`: meaningful signals that deserve follow-up
- `inconclusive`: checks were limited, failed, or only weak heuristics fired
- `likely_authentic`: no strong evidence was found across the available checks

`likely_authentic` does not mean proven authentic.

## Smoke Test

Run the end-to-end smoke test:

```bash
./test.sh
```

It builds the container, uploads sample images, checks the main pages and JSON endpoints, verifies generated analysis artifacts, and cleans up afterward.

## Data Storage

Runtime data is stored in `./data/`:

- `data/uploads/` original uploads
- `data/results/` rendered analysis artifacts and result JSON
- `data/history.json` recent analysis history

## Privacy Notes

- Do not upload sensitive images to an internet-exposed instance unless you understand the storage model.
- Results and original files remain on disk until you clear them.
- Reverse image search links open external services in a new tab; manual upload to those services is up to the user.

## License

MIT
