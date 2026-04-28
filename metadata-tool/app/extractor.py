import subprocess
import json
import os
import re
import random
import magic
from app.settings import load_settings, is_value_allowed

# Tags that constitute PII or privacy-sensitive metadata
PII_TAGS = {
    # GPS / Location
    'GPSLatitude', 'GPSLongitude', 'GPSPosition', 'GPSAltitude',
    'GPSLatitudeRef', 'GPSLongitudeRef', 'GPSCoordinates',
    'Location', 'LocationName', 'Sub-location', 'City', 'State', 'Country',
    # Identity
    'Author', 'Creator', 'Artist', 'By-line', 'Owner', 'OwnerName',
    'CameraOwnerName', 'Copyright', 'CopyrightNotice', 'Rights',
    'LastModifiedBy', 'RevisionAuthor', 'Manager', 'Company',
    # Device identifiers
    'SerialNumber', 'InternalSerialNumber', 'LensSerialNumber',
    'CameraSerialNumber', 'BodySerialNumber', 'ImageUniqueID',
    'DigitalSourceFileID', 'OriginalDocumentID', 'DocumentID',
    'InstanceID', 'UniqueID', 'DeviceSerialNumber',
    # Software / system
    'HostComputer', 'Software', 'CreatorTool', 'Producer',
    'ApplicationName', 'ProgramName',
    # Network / paths
    'OriginalFilename', 'Directory', 'FilePath',
    # Timestamps (selectively — creation/modification are useful intel)
    'DateTimeOriginal', 'CreateDate', 'ModifyDate', 'MetadataDate',
    # Comments that might contain PII
    'Comment', 'UserComment', 'ImageDescription', 'Description',
    'Subject', 'Title', 'Headline',
    # Email / contact embedded in docs
    'Emails', 'LastPrinted', 'LastSavedBy',
}

# Broader categories for grouping
PII_CATEGORIES = {
    'gps': {'GPSLatitude', 'GPSLongitude', 'GPSPosition', 'GPSAltitude',
             'GPSLatitudeRef', 'GPSLongitudeRef', 'GPSCoordinates',
             'Location', 'LocationName', 'Sub-location', 'City', 'State', 'Country'},
    'identity': {'Author', 'Creator', 'Artist', 'By-line', 'Owner', 'OwnerName',
                  'CameraOwnerName', 'Copyright', 'CopyrightNotice', 'Rights',
                  'LastModifiedBy', 'RevisionAuthor', 'Manager', 'Company',
                  'LastSavedBy', 'Emails'},
    'device': {'SerialNumber', 'InternalSerialNumber', 'LensSerialNumber',
                'CameraSerialNumber', 'BodySerialNumber', 'ImageUniqueID',
                'DeviceSerialNumber', 'HostComputer'},
    'software': {'Software', 'CreatorTool', 'Producer', 'ApplicationName', 'ProgramName'},
    'tracking_ids': {'DigitalSourceFileID', 'OriginalDocumentID', 'DocumentID',
                      'InstanceID', 'UniqueID'},
    'timestamps': {'DateTimeOriginal', 'CreateDate', 'ModifyDate', 'MetadataDate',
                    'LastPrinted'},
    'content': {'Comment', 'UserComment', 'ImageDescription', 'Description',
                 'Subject', 'Title', 'Headline'},
    'paths': {'OriginalFilename', 'Directory', 'FilePath'},
}


def get_file_type(filepath):
    """Detect MIME type using libmagic."""
    try:
        mime = magic.from_file(filepath, mime=True)
        return mime
    except Exception:
        return 'application/octet-stream'


def get_file_category(mime_type):
    """Map MIME type to a simple category."""
    if not mime_type:
        return 'unknown'
    if mime_type.startswith('image/'):
        return 'image'
    elif mime_type.startswith('video/'):
        return 'video'
    elif mime_type.startswith('audio/'):
        return 'audio'
    elif mime_type in ('application/pdf',):
        return 'pdf'
    elif 'document' in mime_type or 'word' in mime_type or 'spreadsheet' in mime_type or 'presentation' in mime_type:
        return 'document'
    elif 'text' in mime_type:
        return 'text'
    else:
        return 'other'


def run_exiftool(filepath):
    """Run exiftool and return parsed JSON metadata."""
    try:
        result = subprocess.run(
            ['exiftool', '-json', '-a', '-G', '-n', filepath],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return data[0] if data else {}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        pass
    return {}


def parse_gps(raw_meta):
    """Extract GPS coordinates from exiftool output."""
    lat = None
    lon = None

    for key, val in raw_meta.items():
        tag = key.split(':')[-1] if ':' in key else key
        if tag == 'GPSLatitude' and isinstance(val, (int, float)):
            lat = float(val)
        elif tag == 'GPSLongitude' and isinstance(val, (int, float)):
            lon = float(val)

    if lat is not None and lon is not None:
        # Sanity check
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon

    return None, None


def classify_pii(tag_name, tag_value=None, settings=None):
    """Check if a tag is PII and return its categories, respecting settings."""
    clean_name = tag_name.split(':')[-1] if ':' in tag_name else tag_name

    if clean_name not in PII_TAGS:
        return False, []

    if settings is None:
        settings = load_settings()

    enabled = settings.get('enabled_categories', [])

    # Find which categories this tag belongs to
    categories = []
    for cat, tags in PII_CATEGORIES.items():
        if clean_name in tags and cat in enabled:
            categories.append(cat)

    # If tag is PII but none of its categories are enabled, not PII
    if not categories:
        # Check if it would match ANY category (even disabled ones)
        any_cat = False
        for cat, tags in PII_CATEGORIES.items():
            if clean_name in tags:
                any_cat = True
                break
        # If it's in a known category but that category is disabled, skip it
        if any_cat:
            return False, []
        # Unknown category — only flag if 'other' would make sense
        # For now, don't flag uncategorized tags when categories are being filtered
        return False, []

    # Check value allowlist
    if tag_value and is_value_allowed(tag_value):
        return False, []

    return True, categories


def extract_metadata(filepath, original_name):
    """Full metadata extraction pipeline for a single file."""
    file_size = os.path.getsize(filepath)
    mime_type = get_file_type(filepath)
    file_type = get_file_category(mime_type)

    raw_meta = run_exiftool(filepath)

    # Load settings once for this extraction
    settings = load_settings()

    # Parse into structured fields
    fields = []
    pii_flags = set()
    all_meta = {}

    # Tags to skip (exiftool internal / not useful)
    skip_tags = {'SourceFile', 'ExifToolVersion', 'FileName', 'Directory', 'FilePermissions'}

    for key, value in raw_meta.items():
        # exiftool with -G gives "Group:TagName" format
        if ':' in key:
            group, tag_name = key.split(':', 1)
        else:
            group = 'Other'
            tag_name = key

        if tag_name in skip_tags:
            continue

        str_value = str(value) if value is not None else ''
        if not str_value or str_value == '(Binary data)':
            continue

        is_pii, categories = classify_pii(key, str_value, settings)
        if is_pii:
            pii_flags.update(categories)

        fields.append({
            'group': group,
            'name': tag_name,
            'value': str_value,
            'is_pii': 1 if is_pii else 0,
        })

        all_meta[key] = str_value

    # GPS
    gps_lat, gps_lon = parse_gps(raw_meta)

    if gps_lat is not None:
        pii_flags.add('gps')

    return {
        'original_name': original_name,
        'file_size': file_size,
        'file_type': file_type,
        'mime_type': mime_type,
        'has_gps': 1 if gps_lat is not None else 0,
        'gps_lat': gps_lat,
        'gps_lon': gps_lon,
        'pii_flags': list(pii_flags),
        'metadata': all_meta,
        'fields': fields,
    }


def strip_metadata(filepath, output_path):
    """Strip all metadata from a file using exiftool. Preserves original."""
    try:
        import shutil
        shutil.copy2(filepath, output_path)
        result = subprocess.run(
            ['exiftool', '-all=', '-overwrite_original', output_path],
            capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0
    except Exception:
        return False


# Plausible random metadata pools
_CAMERA_MAKES = [
    'Canon', 'Nikon', 'Sony', 'Fujifilm', 'Panasonic', 'Olympus',
    'Leica', 'Pentax', 'Samsung', 'Hasselblad', 'Ricoh', 'Sigma',
]

_CAMERA_MODELS = {
    'Canon': ['EOS R5', 'EOS R6 Mark II', 'EOS 5D Mark IV', 'EOS 90D', 'PowerShot G7 X III'],
    'Nikon': ['Z9', 'Z8', 'Z6 III', 'D850', 'D7500', 'Coolpix P1000'],
    'Sony': ['ILCE-7RM5', 'ILCE-7M4', 'ILCE-6700', 'DSC-RX100M7', 'ILCE-1'],
    'Fujifilm': ['X-T5', 'X-H2S', 'X100VI', 'GFX 100S II', 'X-S20'],
    'Panasonic': ['DC-S5M2', 'DC-GH6', 'DC-G9M2', 'DMC-LX100M2'],
    'Olympus': ['E-M1 Mark III', 'E-M5 Mark III', 'PEN E-P7'],
    'Leica': ['M11', 'Q3', 'SL2-S', 'CL'],
    'Pentax': ['K-3 Mark III', 'K-1 Mark II', 'KF'],
    'Samsung': ['NX1', 'NX500', 'WB2200F'],
    'Hasselblad': ['X2D 100C', 'X1D II 50C', '907X 50C'],
    'Ricoh': ['GR IIIx', 'GR III', 'Theta Z1'],
    'Sigma': ['fp L', 'fp', 'dp Quattro'],
}

_LENS_NAMES = [
    '24-70mm f/2.8', '70-200mm f/2.8', '50mm f/1.4', '35mm f/1.8',
    '85mm f/1.8', '16-35mm f/4', '100-400mm f/4.5-5.6', '28mm f/2',
    '14-24mm f/2.8', '55mm f/1.2', '24-105mm f/4', '135mm f/1.8',
    '20mm f/1.8', '40mm f/2.8', '90mm f/2.8 Macro',
]

_SOFTWARE = [
    'Adobe Photoshop 25.6', 'Adobe Lightroom Classic 13.2',
    'Adobe Photoshop Lightroom 7.4', 'Capture One 23',
    'DxO PhotoLab 7', 'GIMP 2.10.36', 'Affinity Photo 2',
    'Darktable 4.6.1', 'RawTherapee 5.10', 'Luminar Neo 1.18',
    'Pixelmator Pro 3.5', 'ON1 Photo RAW 2024',
]

_CITIES = [
    ('Tokyo', 'Japan', 35.6762, 139.6503),
    ('London', 'United Kingdom', 51.5074, -0.1278),
    ('New York', 'United States', 40.7128, -74.0060),
    ('Paris', 'France', 48.8566, 2.3522),
    ('Sydney', 'Australia', -33.8688, 151.2093),
    ('Berlin', 'Germany', 52.5200, 13.4050),
    ('Toronto', 'Canada', 43.6532, -79.3832),
    ('Singapore', 'Singapore', 1.3521, 103.8198),
    ('Dubai', 'UAE', 25.2048, 55.2708),
    ('Seoul', 'South Korea', 37.5665, 126.9780),
    ('Mumbai', 'India', 19.0760, 72.8777),
    ('Stockholm', 'Sweden', 59.3293, 18.0686),
    ('Buenos Aires', 'Argentina', -34.6037, -58.3816),
    ('Cape Town', 'South Africa', -33.9249, 18.4241),
    ('Bangkok', 'Thailand', 13.7563, 100.5018),
    ('Mexico City', 'Mexico', 19.4326, -99.1332),
    ('Istanbul', 'Turkey', 41.0082, 28.9784),
    ('Warsaw', 'Poland', 52.2297, 21.0122),
    ('Nairobi', 'Kenya', -1.2921, 36.8219),
    ('Lima', 'Peru', -12.0464, -77.0428),
]


def _random_date():
    """Generate a plausible random date in the last 3 years."""
    year = random.randint(2022, 2026)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    hour = random.randint(0, 23)
    minute = random.randint(0, 59)
    second = random.randint(0, 59)
    return f"{year}:{month:02d}:{day:02d} {hour:02d}:{minute:02d}:{second:02d}"


def _random_serial():
    """Generate a plausible camera serial number."""
    prefix = random.choice(['', 'B', 'C', 'D', 'E', 'N', 'S'])
    digits = ''.join([str(random.randint(0, 9)) for _ in range(random.randint(8, 12))])
    return f"{prefix}{digits}"


def randomize_metadata(filepath, output_path):
    """Strip real metadata and inject plausible random EXIF data."""
    # First strip everything
    if not strip_metadata(filepath, output_path):
        return False

    make = random.choice(_CAMERA_MAKES)
    model = random.choice(_CAMERA_MODELS.get(make, ['Unknown']))
    lens = random.choice(_LENS_NAMES)
    software = random.choice(_SOFTWARE)
    date = _random_date()
    serial = _random_serial()

    # Build exiftool args for injection
    args = [
        'exiftool',
        '-overwrite_original',
        f'-Make={make}',
        f'-Model={model}',
        f'-LensModel={lens}',
        f'-Software={software}',
        f'-DateTimeOriginal={date}',
        f'-CreateDate={date}',
        f'-ModifyDate={date}',
        f'-SerialNumber={serial}',
        f'-ISO={random.choice([100, 200, 400, 800, 1600, 3200])}',
        f'-FocalLength={random.choice([14, 24, 28, 35, 50, 70, 85, 100, 135, 200])}',
        f'-FNumber={random.choice([1.4, 1.8, 2.0, 2.8, 4.0, 5.6, 8.0, 11.0])}',
        f'-ExposureTime={random.choice(["1/30", "1/60", "1/125", "1/250", "1/500", "1/1000", "1/2000"])}',
        f'-WhiteBalance={random.choice(["Auto", "Daylight", "Cloudy", "Tungsten", "Fluorescent", "Flash"])}',
    ]

    # Randomly add GPS (50% chance)
    if random.random() < 0.5:
        city, country, lat, lon = random.choice(_CITIES)
        # Add some jitter (within ~5km)
        lat += random.uniform(-0.05, 0.05)
        lon += random.uniform(-0.05, 0.05)
        lat_ref = 'N' if lat >= 0 else 'S'
        lon_ref = 'E' if lon >= 0 else 'W'
        args.extend([
            f'-GPSLatitude={abs(lat)}',
            f'-GPSLatitudeRef={lat_ref}',
            f'-GPSLongitude={abs(lon)}',
            f'-GPSLongitudeRef={lon_ref}',
        ])

    args.append(output_path)

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except Exception:
        return False
