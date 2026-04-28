import os
import json

SETTINGS_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'settings.json')

# All PII categories with descriptions
ALL_CATEGORIES = {
    'gps': {
        'label': 'GPS / Location',
        'description': 'GPS coordinates, city, state, country, location names',
        'default': True,
    },
    'identity': {
        'label': 'Identity',
        'description': 'Author, creator, owner, copyright holder, company names',
        'default': True,
    },
    'device': {
        'label': 'Device IDs',
        'description': 'Camera/device serial numbers, host computer name',
        'default': True,
    },
    'software': {
        'label': 'Software',
        'description': 'Software name, creator tool, producer application',
        'default': False,
    },
    'tracking_ids': {
        'label': 'Tracking IDs',
        'description': 'Document IDs, instance IDs, unique identifiers',
        'default': True,
    },
    'timestamps': {
        'label': 'Timestamps',
        'description': 'Creation date, modification date, original date/time',
        'default': False,
    },
    'content': {
        'label': 'Content / Comments',
        'description': 'User comments, image description, title, subject, headline',
        'default': True,
    },
    'paths': {
        'label': 'File Paths',
        'description': 'Original filename, directory path',
        'default': True,
    },
}

DEFAULT_SETTINGS = {
    'enabled_categories': [k for k, v in ALL_CATEGORIES.items() if v['default']],
    'value_allowlist': [
        'Screenshot',
        'screenshot',
    ],
}


def _ensure_dir():
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)


def load_settings():
    _ensure_dir()
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, 'r') as f:
                saved = json.load(f)
            # Merge with defaults for any new keys
            settings = dict(DEFAULT_SETTINGS)
            settings.update(saved)
            return settings
        except (json.JSONDecodeError, Exception):
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(settings):
    _ensure_dir()
    with open(SETTINGS_PATH, 'w') as f:
        json.dump(settings, f, indent=2)


def is_pii_enabled(category):
    settings = load_settings()
    return category in settings.get('enabled_categories', [])


def is_value_allowed(value):
    settings = load_settings()
    allowlist = settings.get('value_allowlist', [])
    val_stripped = value.strip()
    for allowed in allowlist:
        if val_stripped.lower() == allowed.lower():
            return True
    return False
