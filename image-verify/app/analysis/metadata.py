"""
Metadata integrity analysis.

Checks:
- Whether metadata has been stripped (suspiciously bare)
- Whether metadata fields are internally consistent
- Known software signatures that indicate editing
- Mismatches between claimed camera and actual image properties
"""

import subprocess
import json
# Known editing software signatures
EDITING_SOFTWARE = [
    'photoshop', 'gimp', 'lightroom', 'affinity', 'pixelmator',
    'capture one', 'darktable', 'rawtherapee', 'luminar',
    'snapseed', 'vsco', 'canva', 'paint.net', 'corel',
    'on1', 'dxo', 'topaz', 'skylum',
]

# Known AI generation signatures
AI_GENERATION_SOFTWARE = [
    'midjourney', 'dall-e', 'dalle', 'stable diffusion', 'comfyui',
    'automatic1111', 'invoke ai', 'novelai', 'firefly', 'ideogram',
    'leonardo', 'playground', 'flux',
]

# Camera makes that should have specific EXIF patterns
KNOWN_CAMERA_MAKES = {
    'canon', 'nikon', 'sony', 'fujifilm', 'panasonic', 'olympus',
    'leica', 'pentax', 'samsung', 'hasselblad', 'ricoh', 'sigma',
    'apple', 'google', 'huawei', 'xiaomi', 'oppo', 'oneplus',
}


def run_exiftool(filepath):
    try:
        result = subprocess.run(
            ['exiftool', '-json', '-a', '-G', '-n', filepath],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return data[0] if data else {}
    except Exception:
        pass
    return {}


def analyze_metadata(filepath):
    """Full metadata integrity analysis."""
    raw = run_exiftool(filepath)

    if not raw:
        return {
            'status': 'error',
            'summary': 'Could not read metadata.',
            'findings': [],
            'fields': {},
            'field_count': 0,
        }

    # Flatten tag names
    fields = {}
    for key, val in raw.items():
        tag = key.split(':')[-1] if ':' in key else key
        if tag in ('SourceFile', 'ExifToolVersion', 'FileName',
                   'Directory', 'FilePermissions'):
            continue
        fields[tag] = str(val) if val is not None else ''

    findings = []
    severity_scores = []  # 0=info, 1=note, 2=suspicious, 3=concerning

    # ── Check 1: Metadata stripped? ──
    exif_fields = [k for k in raw if k.startswith('EXIF:')]
    file_type = fields.get('FileType', '').upper()
    has_camera_claim = bool(fields.get('Make', '').strip() or fields.get('Model', '').strip())

    if len(exif_fields) == 0 and file_type in ('JPEG', 'JPG', 'TIFF', 'HEIC', 'HEIF'):
        findings.append({
            'check': 'Metadata presence',
            'result': 'No EXIF data found',
            'detail': 'This image has no EXIF metadata. It may have been stripped intentionally, '
                      'or saved/exported by software that does not preserve metadata. '
                      'This is common for screenshots, social media re-exports, and privacy-focused workflows.',
            'severity': 'note',
        })
        severity_scores.append(1)
    elif len(exif_fields) < 5 and file_type in ('JPEG', 'JPG'):
        findings.append({
            'check': 'Metadata presence',
            'result': 'Minimal EXIF data',
            'detail': f'Only {len(exif_fields)} EXIF field(s) found. Most cameras write 30+. '
                      'Metadata may have been partially stripped, but lightweight exports often look like this too.',
            'severity': 'note',
        })
        severity_scores.append(1)
    else:
        findings.append({
            'check': 'Metadata presence',
            'result': f'{len(exif_fields)} EXIF fields found',
            'detail': 'Metadata appears intact.',
            'severity': 'ok',
        })
        severity_scores.append(0)

    # ── Check 2: Editing software detected? ──
    software_fields = [
        fields.get('Software', ''),
        fields.get('CreatorTool', ''),
        fields.get('Producer', ''),
        fields.get('ApplicationName', ''),
        fields.get('ProcessingSoftware', ''),
        fields.get('HistorySoftwareAgent', ''),
    ]
    software_str = ' '.join(software_fields).lower()

    ai_detected = [s for s in AI_GENERATION_SOFTWARE if s in software_str]
    edit_detected = [s for s in EDITING_SOFTWARE if s in software_str]

    if ai_detected:
        findings.append({
            'check': 'Software signatures',
            'result': f'AI generation software detected: {", ".join(ai_detected)}',
            'detail': 'Metadata indicates this image was created by an AI image generator.',
            'severity': 'concerning',
        })
        severity_scores.append(3)
    elif edit_detected:
        findings.append({
            'check': 'Software signatures',
            'result': f'Editing software detected: {", ".join(edit_detected)}',
            'detail': 'This image has been processed through editing software. '
                      'This is normal for professional photography but indicates the image is not straight from camera.',
            'severity': 'note',
        })
        severity_scores.append(1)
    elif software_str.strip():
        findings.append({
            'check': 'Software signatures',
            'result': f'Software: {software_str.strip()[:100]}',
            'detail': 'Software field present but not a known editor or AI tool.',
            'severity': 'ok',
        })
        severity_scores.append(0)

    # ── Check 3: Camera make/model consistency ──
    make = fields.get('Make', '').strip()
    model = fields.get('Model', '').strip()

    if make and make.lower() in KNOWN_CAMERA_MAKES:
        # Check for fields a real camera should have
        expected_fields = ['ExposureTime', 'FNumber', 'ISO', 'FocalLength']
        missing = [f for f in expected_fields if f not in fields]
        if len(missing) >= 3 and not edit_detected and not ai_detected:
            findings.append({
                'check': 'Camera consistency',
                'result': f'Claims {make} {model} but missing core camera fields',
                'detail': f'Camera make/model is set to {make} {model}, but {len(missing)} of 4 '
                          f'core shooting parameters are missing ({", ".join(missing)}). '
                          'Real cameras usually write these, but exports and metadata sanitization can remove them. '
                          'Treat this as suspicious, not conclusive.',
                'severity': 'suspicious',
            })
            severity_scores.append(2)
        else:
            findings.append({
                'check': 'Camera consistency',
                'result': f'{make} {model} — shooting parameters present',
                'detail': 'Camera metadata appears consistent with a real capture.',
                'severity': 'ok',
            })
            severity_scores.append(0)
    elif make:
        findings.append({
            'check': 'Camera consistency',
            'result': f'Unknown camera make: {make}',
            'detail': 'Camera make is not in the known list. This does not indicate tampering.',
            'severity': 'info',
        })
        severity_scores.append(0)

    # ── Check 4: Date consistency ──
    dates = {}
    for field in ['DateTimeOriginal', 'CreateDate', 'ModifyDate', 'FileModifyDate']:
        val = fields.get(field, '')
        if val and val != '0000:00:00 00:00:00':
            dates[field] = val

    if 'DateTimeOriginal' in dates and 'ModifyDate' in dates:
        orig = dates['DateTimeOriginal'][:10]
        mod = dates['ModifyDate'][:10]
        if orig != mod:
            findings.append({
                'check': 'Date consistency',
                'result': f'Original date ({orig}) differs from modify date ({mod})',
                'detail': 'The file has been modified after the original capture date. '
                          'This is common for edited photos but worth noting.',
                'severity': 'note',
            })
            severity_scores.append(1)
        else:
            findings.append({
                'check': 'Date consistency',
                'result': 'Capture and modify dates match',
                'detail': 'Dates are consistent.',
                'severity': 'ok',
            })
            severity_scores.append(0)
    elif len(dates) == 0 and file_type in ('JPEG', 'JPG') and has_camera_claim:
        findings.append({
            'check': 'Date consistency',
            'result': 'No date metadata found',
            'detail': 'No date fields present even though the image claims a camera source. '
                      'Dates may have been stripped during export.',
            'severity': 'note',
        })
        severity_scores.append(1)

    # ── Check 5: GPS presence ──
    has_gps = 'GPSLatitude' in fields and 'GPSLongitude' in fields
    if has_gps:
        lat = fields.get('GPSLatitude', '')
        lon = fields.get('GPSLongitude', '')
        findings.append({
            'check': 'GPS data',
            'result': f'GPS coordinates present: {lat}, {lon}',
            'detail': 'Image contains GPS location data. This is typical for phone cameras.',
            'severity': 'info',
        })

    # ── Check 6: ICC profile mismatch ──
    icc_desc = fields.get('ProfileDescription', '').lower()
    color_space = fields.get('ColorSpace', '')
    if make and 'srgb' not in icc_desc and 'display p3' not in icc_desc and icc_desc:
        # Unusual ICC profile for a consumer camera
        if make.lower() in ('apple', 'samsung', 'google', 'huawei', 'xiaomi'):
            findings.append({
                'check': 'Color profile',
                'result': f'Unusual ICC profile for {make}: {icc_desc[:60]}',
                'detail': 'Some phone-camera workflows, especially on Apple devices and certain '
                          'modern Android devices, use sRGB or Display P3 profiles. An unusual '
                          'profile may indicate re-export or alternate processing, but this is not conclusive.',
                'severity': 'note',
            })
            severity_scores.append(1)

    # ── Overall assessment ──
    max_severity = max(severity_scores) if severity_scores else 0
    if max_severity >= 3:
        status = 'concerning'
        summary = 'Metadata includes strong software signals consistent with AI generation or major processing.'
    elif max_severity >= 2:
        status = 'suspicious'
        summary = 'Metadata has inconsistencies that warrant further investigation.'
    elif max_severity >= 1:
        status = 'note'
        summary = 'Metadata shows signs of editing or partial stripping, but nothing conclusive.'
    else:
        status = 'ok'
        summary = 'Metadata appears intact and internally consistent.'

    return {
        'status': status,
        'summary': summary,
        'findings': findings,
        'fields': fields,
        'field_count': len(fields),
        'exif_count': len(exif_fields),
        'has_gps': has_gps,
        'gps_lat': fields.get('GPSLatitude'),
        'gps_lon': fields.get('GPSLongitude'),
        'dates': dates,
        'software': software_str.strip() or None,
        'camera': f"{make} {model}".strip() or None,
    }
