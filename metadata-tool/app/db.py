import sqlite3
import os
import json
from app.extractor import PII_TAGS

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'metadata.db')


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            file_size INTEGER,
            file_type TEXT,
            mime_type TEXT,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            has_gps INTEGER DEFAULT 0,
            gps_lat REAL,
            gps_lon REAL,
            pii_flags TEXT DEFAULT '[]',
            metadata_json TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS metadata_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            tag_group TEXT,
            tag_name TEXT NOT NULL,
            tag_value TEXT,
            is_pii INTEGER DEFAULT 0,
            FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_files_has_gps ON files(has_gps);
        CREATE INDEX IF NOT EXISTS idx_fields_file_id ON metadata_fields(file_id);
        CREATE INDEX IF NOT EXISTS idx_fields_tag_name ON metadata_fields(tag_name);
    """)
    conn.commit()
    conn.close()


def insert_file(file_data):
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO files (filename, original_name, file_size, file_type, mime_type,
                          has_gps, gps_lat, gps_lon, pii_flags, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        file_data['filename'],
        file_data['original_name'],
        file_data['file_size'],
        file_data['file_type'],
        file_data['mime_type'],
        file_data.get('has_gps', 0),
        file_data.get('gps_lat'),
        file_data.get('gps_lon'),
        json.dumps(file_data.get('pii_flags', [])),
        json.dumps(file_data.get('metadata', {}))
    ))
    file_id = cur.lastrowid

    for field in file_data.get('fields', []):
        conn.execute("""
            INSERT INTO metadata_fields (file_id, tag_group, tag_name, tag_value, is_pii)
            VALUES (?, ?, ?, ?, ?)
        """, (file_id, field['group'], field['name'], field['value'], field.get('is_pii', 0)))

    conn.commit()
    conn.close()
    return file_id


def get_file(file_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        conn.close()
        return None

    fields = conn.execute(
        "SELECT * FROM metadata_fields WHERE file_id = ? ORDER BY tag_group, tag_name",
        (file_id,)
    ).fetchall()
    conn.close()

    result = dict(row)
    result['pii_flags'] = json.loads(result['pii_flags'])
    result['metadata'] = json.loads(result['metadata_json'])
    result['fields'] = [dict(f) for f in fields]
    return result


def get_all_files(page=1, per_page=50, gps_only=False, pii_only=False, search=None):
    conn = get_db()
    conditions = []
    params = []

    if gps_only:
        conditions.append("has_gps = 1")
    if pii_only:
        conditions.append("id IN (SELECT DISTINCT file_id FROM metadata_fields WHERE is_pii = 1)")
    if search:
        conditions.append("(original_name LIKE ? OR metadata_json LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    offset = (page - 1) * per_page

    total = conn.execute(f"SELECT COUNT(*) FROM files {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM files {where} ORDER BY uploaded_at DESC LIMIT ? OFFSET ?",
        params + [per_page, offset]
    ).fetchall()
    conn.close()

    files = []
    for row in rows:
        f = dict(row)
        f['pii_flags'] = json.loads(f['pii_flags'])
        files.append(f)

    return {'files': files, 'total': total, 'page': page, 'pages': (total + per_page - 1) // per_page}


def get_recent_images(limit=12):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, filename, original_name FROM files WHERE file_type = 'image' ORDER BY uploaded_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM files WHERE file_type = 'image'").fetchone()[0]
    conn.close()
    return [dict(r) for r in rows], total


def get_gps_files():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, original_name, gps_lat, gps_lon, file_type, uploaded_at FROM files WHERE has_gps = 1"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_file(file_id):
    conn = get_db()
    conn.execute("DELETE FROM metadata_fields WHERE file_id = ?", (file_id,))
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    conn.commit()
    conn.close()


def delete_all_files():
    conn = get_db()
    conn.execute("DELETE FROM metadata_fields")
    conn.execute("DELETE FROM files")
    conn.commit()
    conn.close()


def reprocess_pii(classify_fn, settings):
    """Re-evaluate is_pii on all metadata fields using current settings."""
    conn = get_db()
    rows = conn.execute("SELECT id, tag_name, tag_value FROM metadata_fields").fetchall()
    for row in rows:
        is_pii, _ = classify_fn(row['tag_name'], row['tag_value'], settings)
        conn.execute("UPDATE metadata_fields SET is_pii = ? WHERE id = ?",
                     (1 if is_pii else 0, row['id']))
    conn.commit()
    conn.close()


def get_stats():
    conn = get_db()
    stats = {
        'total_files': conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        'gps_files': conn.execute("SELECT COUNT(*) FROM files WHERE has_gps = 1").fetchone()[0],
        'pii_files': conn.execute("SELECT COUNT(DISTINCT file_id) FROM metadata_fields WHERE is_pii = 1").fetchone()[0],
        'total_fields': conn.execute("SELECT COUNT(*) FROM metadata_fields").fetchone()[0],
        'pii_fields': conn.execute("SELECT COUNT(*) FROM metadata_fields WHERE is_pii = 1").fetchone()[0],
    }

    # Count fields that match PII tag names but aren't currently flagged
    placeholders = ','.join('?' for _ in PII_TAGS)
    total_potential = conn.execute(
        f"SELECT COUNT(*) FROM metadata_fields WHERE tag_name IN ({placeholders})",
        list(PII_TAGS)
    ).fetchone()[0]
    stats['excluded_pii'] = total_potential - stats['pii_fields']

    top_types = conn.execute(
        "SELECT file_type, COUNT(*) as cnt FROM files GROUP BY file_type ORDER BY cnt DESC LIMIT 10"
    ).fetchall()
    stats['top_types'] = [{'type': r['file_type'], 'count': r['cnt']} for r in top_types]

    conn.close()
    return stats
