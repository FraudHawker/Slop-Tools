import os
import uuid
import csv
import json
import io
import shutil
import zipfile
import tempfile
from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, send_file, flash, abort)
from werkzeug.utils import secure_filename

from app.db import init_db, insert_file, get_file, get_all_files, get_gps_files, get_recent_images, delete_file, delete_all_files, get_stats, reprocess_pii
from app.extractor import extract_metadata, strip_metadata, randomize_metadata, classify_pii
from app.settings import load_settings, save_settings, ALL_CATEGORIES

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-me-in-production')

MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', 100))
MAX_TOTAL_GB = int(os.environ.get('MAX_TOTAL_GB', 10))
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024
MAX_TOTAL_BYTES = MAX_TOTAL_GB * 1024 * 1024 * 1024

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'uploads')
CLEAN_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'clean')
THUMB_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'thumbs')
RANDOMIZED_DIR = os.path.join(os.path.dirname(__file__), '..', 'data', 'randomized')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CLEAN_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)
os.makedirs(RANDOMIZED_DIR, exist_ok=True)

init_db()


def get_total_storage_bytes():
    total = 0
    data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    for root, _, files in os.walk(data_dir):
        for filename in files:
            path = os.path.join(root, filename)
            try:
                total += os.path.getsize(path)
            except OSError:
                continue
    return total


def find_thumbnail_path(stored_name):
    stem = os.path.splitext(stored_name)[0]
    for ext in ('.jpg', '.png'):
        candidate = os.path.join(THUMB_DIR, f'{stem}{ext}')
        if os.path.exists(candidate):
            return candidate
    return os.path.join(THUMB_DIR, stored_name)


@app.errorhandler(413)
def upload_too_large(_error):
    flash(f'Upload exceeds the {MAX_UPLOAD_MB} MB per-file limit.', 'error')
    return redirect(url_for('index'))


@app.route('/')
def index():
    page = request.args.get('page', 1, type=int)
    gps_only = request.args.get('gps', '') == '1'
    pii_only = request.args.get('pii', '') == '1'
    search = request.args.get('q', '').strip() or None
    result = get_all_files(page=page, gps_only=gps_only, pii_only=pii_only, search=search)
    stats = get_stats()
    recent_images, total_images = get_recent_images(limit=12)
    return render_template('index.html', **result, stats=stats,
                          recent_images=recent_images, total_images=total_images,
                          gps_only=gps_only, pii_only=pii_only, search=search or '')


@app.route('/upload', methods=['POST'])
def upload():
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        flash('No files selected.', 'error')
        return redirect(url_for('index'))

    count = 0
    errors = 0
    for f in files:
        if f.filename == '':
            continue
        filepath = None
        try:
            original_name = secure_filename(f.filename) or 'unnamed'
            ext = os.path.splitext(original_name)[1]
            stored_name = f"{uuid.uuid4().hex}{ext}"
            filepath = os.path.join(UPLOAD_DIR, stored_name)
            f.save(filepath)

            file_size = os.path.getsize(filepath)
            if get_total_storage_bytes() > MAX_TOTAL_BYTES:
                os.remove(filepath)
                errors += 1
                flash(
                    f'Skipped "{original_name}": total storage limit of {MAX_TOTAL_GB} GB reached.',
                    'error'
                )
                continue

            file_data = extract_metadata(filepath, original_name)
            file_data['filename'] = stored_name
            file_data['file_size'] = file_size
            insert_file(file_data)
            count += 1
        except Exception as e:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
            errors += 1
            app.logger.error(f"Error processing {f.filename}: {e}")

    if count:
        flash(f'Processed {count} file{"s" if count != 1 else ""}.', 'success')
    if errors:
        flash(f'Failed to process {errors} file{"s" if errors != 1 else ""}.', 'error')

    return redirect(url_for('index'))


@app.route('/wipe', methods=['POST'])
def wipe():
    delete_all_files()
    # Clear upload and thumbnail dirs
    for d in [UPLOAD_DIR, CLEAN_DIR, THUMB_DIR, RANDOMIZED_DIR]:
        if os.path.exists(d):
            shutil.rmtree(d)
        os.makedirs(d, exist_ok=True)
    flash('All files wiped.', 'success')
    return redirect(url_for('index'))


@app.route('/file/<int:file_id>')
def file_detail(file_id):
    f = get_file(file_id)
    if not f:
        abort(404)
    return render_template('detail.html', file=f)


@app.route('/file/<int:file_id>/delete', methods=['POST'])
def file_delete(file_id):
    f = get_file(file_id)
    if not f:
        abort(404)
    # Remove stored file
    filepath = os.path.join(UPLOAD_DIR, f['filename'])
    if os.path.exists(filepath):
        os.remove(filepath)
    clean_path = os.path.join(CLEAN_DIR, f['filename'])
    if os.path.exists(clean_path):
        os.remove(clean_path)
    delete_file(file_id)
    flash('File deleted.', 'success')
    return redirect(url_for('index'))


@app.route('/file/<int:file_id>/strip')
def file_strip(file_id):
    f = get_file(file_id)
    if not f:
        abort(404)
    src = os.path.join(UPLOAD_DIR, f['filename'])
    dst = os.path.join(CLEAN_DIR, f['filename'])
    if not os.path.exists(src):
        abort(404)

    if strip_metadata(src, dst):
        return send_file(dst, as_attachment=True,
                        download_name=f"clean_{f['original_name']}")
    else:
        flash('Failed to strip metadata.', 'error')
        return redirect(url_for('file_detail', file_id=file_id))


@app.route('/file/<int:file_id>/randomize')
def file_randomize(file_id):
    f = get_file(file_id)
    if not f:
        abort(404)
    src = os.path.join(UPLOAD_DIR, f['filename'])
    dst = os.path.join(RANDOMIZED_DIR, f['filename'])
    if not os.path.exists(src):
        abort(404)

    success = randomize_metadata(src, dst)
    if success and os.path.exists(dst):
        return send_file(dst, as_attachment=True,
                        download_name=f"rand_{f['original_name']}")
    else:
        app.logger.error(f"Randomize failed for {f['filename']}: success={success}, dst_exists={os.path.exists(dst)}")
        flash('Failed to randomize metadata. Only image files can be randomized.', 'error')
        return redirect(url_for('file_detail', file_id=file_id))


@app.route('/map')
def map_view():
    points = get_gps_files()
    return render_template('map.html', points=points)


@app.route('/export/clean')
def export_clean():
    result = get_all_files(page=1, per_page=100000)
    if not result['files']:
        flash('No files to export.', 'error')
        return redirect(url_for('index'))

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
    tmp.close()
    stripped = 0
    skipped = 0

    with zipfile.ZipFile(tmp.name, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in result['files']:
            src = os.path.join(UPLOAD_DIR, f['filename'])
            if not os.path.exists(src):
                skipped += 1
                continue
            clean_name = f"clean_{f['original_name']}"
            clean_tmp = os.path.join(CLEAN_DIR, f['filename'])
            if strip_metadata(src, clean_tmp):
                zf.write(clean_tmp, clean_name)
                stripped += 1
            else:
                skipped += 1

    if stripped == 0:
        os.unlink(tmp.name)
        flash('Could not strip any files.', 'error')
        return redirect(url_for('index'))

    return send_file(tmp.name, as_attachment=True,
                    download_name='clean_files.zip',
                    mimetype='application/zip')


@app.route('/export/randomized')
def export_randomized():
    result = get_all_files(page=1, per_page=100000)
    if not result['files']:
        flash('No files to export.', 'error')
        return redirect(url_for('index'))

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
    tmp.close()
    count = 0

    with zipfile.ZipFile(tmp.name, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in result['files']:
            src = os.path.join(UPLOAD_DIR, f['filename'])
            if not os.path.exists(src):
                continue
            dst = os.path.join(RANDOMIZED_DIR, f['filename'])
            if randomize_metadata(src, dst):
                zf.write(dst, f"rand_{f['original_name']}")
                count += 1

    if count == 0:
        os.unlink(tmp.name)
        flash('Could not randomize any files.', 'error')
        return redirect(url_for('index'))

    return send_file(tmp.name, as_attachment=True,
                    download_name='randomized_files.zip',
                    mimetype='application/zip')


@app.route('/export/csv')
def export_csv():
    result = get_all_files(page=1, per_page=100000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['id', 'filename', 'file_type', 'mime_type', 'file_size',
                     'has_gps', 'gps_lat', 'gps_lon', 'pii_flags', 'uploaded_at'])
    for f in result['files']:
        writer.writerow([
            f['id'], f['original_name'], f['file_type'], f['mime_type'],
            f['file_size'], f['has_gps'], f.get('gps_lat', ''), f.get('gps_lon', ''),
            ', '.join(f['pii_flags']), f['uploaded_at']
        ])
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name='metadata_export.csv'
    )


@app.route('/export/json')
def export_json():
    result = get_all_files(page=1, per_page=100000)
    return jsonify(result['files'])


@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    settings = load_settings()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'save_categories':
            enabled = request.form.getlist('categories')
            settings['enabled_categories'] = enabled
            save_settings(settings)
            reprocess_pii(classify_pii, settings)
            flash('PII categories updated. All files reprocessed.', 'success')

        elif action == 'add_allowlist':
            value = request.form.get('value', '').strip()
            if value and value not in settings.get('value_allowlist', []):
                settings.setdefault('value_allowlist', []).append(value)
                save_settings(settings)
                reprocess_pii(classify_pii, settings)
                flash(f'Added "{value}" to allowlist. All files reprocessed.', 'success')

        elif action == 'remove_allowlist':
            value = request.form.get('value', '').strip()
            allowlist = settings.get('value_allowlist', [])
            if value in allowlist:
                allowlist.remove(value)
                settings['value_allowlist'] = allowlist
                save_settings(settings)
                reprocess_pii(classify_pii, settings)
                flash(f'Removed "{value}" from allowlist. All files reprocessed.', 'success')

        return redirect(url_for('settings_page'))

    return render_template('settings.html', settings=settings, all_categories=ALL_CATEGORIES)


@app.route('/api/stats')
def api_stats():
    return jsonify(get_stats())


@app.route('/api/files')
def api_files():
    page = request.args.get('page', 1, type=int)
    result = get_all_files(page=page)
    return jsonify(result)


@app.route('/api/file/<int:file_id>')
def api_file(file_id):
    f = get_file(file_id)
    if not f:
        return jsonify({'error': 'not found'}), 404
    return jsonify(f)


@app.route('/api/gps')
def api_gps():
    return jsonify(get_gps_files())


@app.route('/thumb/<int:file_id>')
def thumbnail(file_id):
    f = get_file(file_id)
    if not f or f['file_type'] != 'image':
        abort(404)
    filepath = os.path.join(UPLOAD_DIR, f['filename'])
    if not os.path.exists(filepath):
        abort(404)

    thumb_path = find_thumbnail_path(f['filename'])

    if not os.path.exists(thumb_path):
        try:
            from PIL import Image as PILImage
            img = PILImage.open(filepath)
            img.thumbnail((200, 200))
            fmt = 'JPEG' if img.mode == 'RGB' else 'PNG'
            if img.mode not in ('RGB', 'RGBA'):
                img = img.convert('RGB')
                fmt = 'JPEG'
            ext = '.jpg' if fmt == 'JPEG' else '.png'
            thumb_path = os.path.join(THUMB_DIR, os.path.splitext(f['filename'])[0] + ext)
            img.save(thumb_path, fmt, quality=80)
        except Exception:
            abort(404)

    return send_file(thumb_path)
