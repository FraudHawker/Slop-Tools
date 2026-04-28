# metadata-tool

Bulk metadata extraction and analysis. Upload files, see what's leaking.

Extracts and catalogs metadata from images, PDFs, Office documents, audio, and video files. Flags privacy-sensitive fields (GPS coordinates, author names, device serial numbers, software identifiers). Provides a searchable web interface, GPS map view, and export options.

## Quick Start

```bash
git clone <this-repo>
cd metadata-tool
docker compose up -d
```

Open `http://localhost:8080`

## What It Does

- **Extracts everything** — runs exiftool against uploaded files, stores all metadata fields in a searchable database
- **Flags PII** — highlights GPS coordinates, author/owner names, device serial numbers, software identifiers, tracking IDs
- **Maps GPS data** — geotagged files appear on a Leaflet map
- **Strips metadata** — download a clean copy of any file with all metadata removed
- **Bulk export** — CSV or JSON export of all extracted data
- **API** — JSON endpoints for programmatic access

## Supported File Types

Anything exiftool can read, which is basically everything:
- Images (JPEG, PNG, TIFF, HEIC, WebP, RAW formats)
- Documents (PDF, DOCX, XLSX, PPTX)
- Audio (MP3, FLAC, WAV, AAC, OGG)
- Video (MP4, MOV, AVI, MKV)
- And hundreds more

## Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 8080 | Host port |
| `MAX_UPLOAD_MB` | 100 | Max single file size in MB |
| `MAX_TOTAL_GB` | 10 | Max total storage in GB |
| `SECRET_KEY` | `change-me-in-production` | Flask session key for flash messages and settings forms |

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/stats` | Summary statistics |
| `GET /api/files?page=1` | Paginated file list |
| `GET /api/file/<id>` | Full metadata for a single file |
| `GET /api/gps` | All geotagged files |
| `GET /export/csv` | CSV export |
| `GET /export/json` | JSON export |

## PII Detection Categories

| Category | Example Fields |
|----------|---------------|
| **gps** | GPSLatitude, GPSLongitude, City, Country |
| **identity** | Author, Creator, CameraOwnerName, Copyright, LastSavedBy |
| **device** | SerialNumber, CameraSerialNumber, HostComputer |
| **software** | Software, CreatorTool, Producer |
| **tracking_ids** | DocumentID, InstanceID, UniqueID |

## Data Storage

All data lives in the `./data/` directory (mounted as a Docker volume):
- `data/metadata.db` — SQLite database
- `data/uploads/` — original uploaded files
- `data/clean/` — stripped copies (generated on demand)

## Testing

Run the end-to-end production smoke test:

```bash
./test.sh
```

It builds the container from scratch, exercises uploads, downloads, exports, settings, filters, and wipes the test data afterward.

## License

MIT
