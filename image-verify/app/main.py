import os
import uuid
import json
import shutil
from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, send_file, flash, abort)
from werkzeug.utils import secure_filename
import magic
import re

# Only allow hex IDs (what we generate)
SAFE_ID_RE = re.compile(r'^[a-f0-9]{12}$')

from app.analysis.metadata import analyze_metadata
from app.analysis.c2pa import analyze_c2pa
from app.analysis.thumbnail import analyze_thumbnail
from app.analysis.forensics import analyze_ela, analyze_noise, analyze_jpeg_ghosts
from app.analysis.reverse_search import get_search_links
from app.examples import examples_exist, generate_all_examples, load_examples, get_examples_dir

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-me-in-production')

MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', 50))
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
RESULTS_DIR = os.path.join(DATA_DIR, 'results')

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)

SUPPORTED_TYPES = {
    'image/jpeg', 'image/png', 'image/tiff', 'image/webp',
    'image/heic', 'image/heif', 'image/avif', 'image/bmp',
}


def validate_id(id_str):
    """Reject any ID that isn't a 12-char hex string."""
    if not SAFE_ID_RE.match(id_str):
        abort(404)
    return id_str


def cleanup_analysis_artifacts(filepath=None, result_dir=None):
    if filepath and os.path.exists(filepath):
        os.remove(filepath)
    if result_dir and os.path.exists(result_dir):
        shutil.rmtree(result_dir, ignore_errors=True)


@app.errorhandler(413)
def upload_too_large(_error):
    flash(f'Upload exceeds the {MAX_UPLOAD_MB} MB limit.', 'error')
    return redirect(url_for('index'))


@app.route('/')
def index():
    # Load recent results
    results = []
    results_file = os.path.join(DATA_DIR, 'history.json')
    if os.path.exists(results_file):
        try:
            with open(results_file, 'r') as f:
                results = json.load(f)
        except Exception:
            pass
    # Load batches
    batches = []
    batch_dir = os.path.join(DATA_DIR, 'batches')
    if os.path.exists(batch_dir):
        for fname in sorted(os.listdir(batch_dir), reverse=True):
            if fname.endswith('.json'):
                try:
                    with open(os.path.join(batch_dir, fname)) as bf:
                        bd = json.load(bf)
                    batches.append({
                        'id': fname.replace('.json', ''),
                        'count': len(bd.get('batch', [])),
                        'files': [r['filename'] for r in bd.get('batch', [])[:3]],
                    })
                except Exception:
                    pass

    return render_template('index.html', results=results[:20], batches=batches[:10])


def save_to_history(entry):
    results_file = os.path.join(DATA_DIR, 'history.json')
    results = []
    if os.path.exists(results_file):
        try:
            with open(results_file, 'r') as f:
                results = json.load(f)
        except Exception:
            pass
    results.insert(0, entry)
    results = results[:100]  # keep last 100
    with open(results_file, 'w') as f:
        json.dump(results, f, indent=2)


def analyze_single_file(f):
    """Analyze a single uploaded file. Returns (analysis_id, error_msg)."""
    original_name = secure_filename(f.filename) or 'unnamed'
    analysis_id = uuid.uuid4().hex[:12]
    ext = os.path.splitext(original_name)[1].lower()
    stored_name = f"{analysis_id}{ext}"
    filepath = os.path.join(UPLOAD_DIR, stored_name)
    f.save(filepath)
    result_dir = None

    # Verify it's an image
    try:
        mime = magic.from_file(filepath, mime=True)
    except Exception:
        mime = 'unknown'

    if mime not in SUPPORTED_TYPES and not mime.startswith('image/'):
        cleanup_analysis_artifacts(filepath=filepath)
        return None, f'"{original_name}" is not a supported image type.'

    # Create results directory for this analysis
    result_dir = os.path.join(RESULTS_DIR, analysis_id)
    os.makedirs(result_dir, exist_ok=True)

    try:
        results = {
            'id': analysis_id,
            'filename': original_name,
            'stored_name': stored_name,
            'mime_type': mime,
            'file_size': os.path.getsize(filepath),
        }

        results['metadata'] = analyze_metadata(filepath)
        results['c2pa'] = analyze_c2pa(filepath)
        results['thumbnail'] = analyze_thumbnail(filepath, output_dir=result_dir)
        results['ela'] = analyze_ela(filepath, output_dir=result_dir)
        results['noise'] = analyze_noise(filepath, output_dir=result_dir)
        results['jpeg_ghosts'] = analyze_jpeg_ghosts(filepath, output_dir=result_dir)
        results['reverse_search'] = get_search_links(filepath)

        results['verdict'] = compute_verdict(results)

        with open(os.path.join(result_dir, 'results.json'), 'w') as rf:
            json.dump(results, rf, indent=2, default=str)

        save_to_history({
            'id': analysis_id,
            'filename': original_name,
            'verdict': results['verdict']['level'],
            'summary': results['verdict']['summary'],
        })

        return analysis_id, None

    except Exception as exc:
        cleanup_analysis_artifacts(filepath=filepath, result_dir=result_dir)
        app.logger.exception("Analysis failed for %s", original_name)
        return None, f'"{original_name}" failed: {type(exc).__name__}'


@app.route('/analyze', methods=['POST'])
def analyze():
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        flash('No files selected.', 'error')
        return redirect(url_for('index'))

    analysis_ids = []
    errors = []

    for f in files:
        if f.filename == '':
            continue
        aid, err = analyze_single_file(f)
        if aid:
            analysis_ids.append(aid)
        if err:
            errors.append(err)

    if errors:
        for err in errors:
            flash(err, 'error')

    if len(analysis_ids) == 1:
        return redirect(url_for('result', analysis_id=analysis_ids[0]))
    elif len(analysis_ids) > 1:
        # Build batch summary and persist it
        batch_id = uuid.uuid4().hex[:12]
        batch = []
        for aid in analysis_ids:
            rfile = os.path.join(RESULTS_DIR, aid, 'results.json')
            if os.path.exists(rfile):
                with open(rfile) as rf:
                    r = json.load(rf)
                batch.append({
                    'id': aid,
                    'filename': r['filename'],
                    'verdict': r['verdict']['level'],
                    'summary': r['verdict']['summary'],
                })
        batch_dir = os.path.join(DATA_DIR, 'batches')
        os.makedirs(batch_dir, exist_ok=True)
        with open(os.path.join(batch_dir, f'{batch_id}.json'), 'w') as bf:
            json.dump({'batch': batch, 'errors': errors}, bf, indent=2)
        return redirect(url_for('batch_result', batch_id=batch_id))
    else:
        return redirect(url_for('index'))


def compute_verdict(results):
    """Aggregate all analysis results into an overall verdict."""

    # Tier 1: Definitive signals (C2PA, thumbnail mismatch)
    c2pa_status = results.get('c2pa', {}).get('status', 'none')
    thumb_status = results.get('thumbnail', {}).get('status', 'none')
    meta_status = results.get('metadata', {}).get('status', 'ok')
    thumb_score = results.get('thumbnail', {}).get('difference_score', 0.0)
    unavailable_checks = []

    if c2pa_status in {'error', 'unavailable'}:
        unavailable_checks.append('C2PA provenance')
    if meta_status == 'error':
        unavailable_checks.append('metadata integrity')
    for label, status in (
        ('thumbnail mismatch', thumb_status),
        ('ELA', results.get('ela', {}).get('status', 'ok')),
        ('noise analysis', results.get('noise', {}).get('status', 'ok')),
        ('JPEG ghosts', results.get('jpeg_ghosts', {}).get('status', 'ok')),
    ):
        if status == 'error':
            unavailable_checks.append(label)

    if unavailable_checks:
        return {
            'level': 'inconclusive',
            'summary': 'Some checks could not be completed, so authenticity could not be assessed confidently.',
            'confidence': 'low',
            'detail': 'Unavailable or failed checks: ' + ', '.join(unavailable_checks) + '. '
                      'Review the per-check details before relying on this result.',
        }

    if c2pa_status == 'verified':
        return {
            'level': 'verified',
            'summary': 'Image has valid C2PA provenance credentials. Cryptographically verified.',
            'confidence': 'high',
            'detail': 'C2PA provides a cryptographic chain of custody from capture to current file. '
                      'Within this tool, this is the strongest available provenance signal.',
        }

    if c2pa_status == 'invalid':
        return {
            'level': 'tampered',
            'summary': 'C2PA credentials present but validation failed. Image may have been modified after signing.',
            'confidence': 'high',
            'detail': 'The C2PA signature chain is broken, indicating modification after the provenance was established.',
        }

    if thumb_status == 'mismatch':
        return {
            'level': 'suspicious',
            'summary': 'Embedded thumbnail differs from the main image.',
            'confidence': 'medium' if thumb_score < 0.30 else 'medium-high',
            'detail': 'Thumbnail mismatch can indicate editing, but legitimate exports, crops, rotation handling, '
                      'or thumbnail regeneration quirks can also cause differences. Treat this as a strong lead, not proof.',
        }

    # Tier 2: Metadata signals
    if meta_status == 'concerning':
        return {
            'level': 'suspicious',
            'summary': results['metadata']['summary'],
            'confidence': 'medium',
            'detail': 'Metadata analysis found concerning signals. See details below.',
        }

    if meta_status == 'suspicious':
        return {
            'level': 'suspicious',
            'summary': results['metadata']['summary'],
            'confidence': 'medium',
            'detail': 'Metadata has inconsistencies. See details below.',
        }

    # Tier 3: Forensic signals (lower confidence)
    ela_status = results.get('ela', {}).get('status', 'ok')
    noise_status = results.get('noise', {}).get('status', 'ok')
    ghost_status = results.get('jpeg_ghosts', {}).get('status', 'ok')

    forensic_flags = sum(1 for s in [ela_status, noise_status, ghost_status] if s == 'note')

    if forensic_flags >= 2:
        return {
            'level': 'inconclusive',
            'summary': 'Multiple forensic analyses flagged potential issues, but these techniques have known limitations.',
            'confidence': 'low',
            'detail': 'ELA, noise, and/or JPEG ghost analysis found anomalies. '
                      'These techniques are unreliable against modern AI editing. '
                      'Treat as leads for further investigation, not conclusions.',
        }

    if forensic_flags == 1:
        return {
            'level': 'inconclusive',
            'summary': 'One forensic check flagged a potential issue. See caveats.',
            'confidence': 'low',
            'detail': 'A single forensic flag is common in normal images and does not indicate tampering on its own.',
        }

    # No flags
    if meta_status == 'note':
        return {
            'level': 'likely_authentic',
            'summary': 'No strong evidence of tampering. Minor metadata notes.',
            'confidence': 'medium',
            'detail': 'Metadata shows minor editing signs (common for processed photos) but '
                      'no forensic analysis found issues. No C2PA provenance available for cryptographic verification.',
        }

    return {
        'level': 'likely_authentic',
        'summary': 'No strong evidence of tampering found across the available checks.',
        'confidence': 'medium',
        'detail': 'All available analyses passed. However, absence of evidence is not evidence of absence — '
                  'sophisticated edits or synthetic images may not be detectable by these techniques. '
                  'No C2PA provenance available for cryptographic verification.',
    }


@app.route('/batch/<batch_id>')
def batch_result(batch_id):
    validate_id(batch_id)
    batch_file = os.path.join(DATA_DIR, 'batches', f'{batch_id}.json')
    if not os.path.exists(batch_file):
        abort(404)
    with open(batch_file) as f:
        data = json.load(f)
    return render_template('batch.html', batch=data['batch'], errors=data.get('errors', []), batch_id=batch_id)


@app.route('/result/<analysis_id>')
def result(analysis_id):
    validate_id(analysis_id)
    result_dir = os.path.join(RESULTS_DIR, analysis_id)
    results_file = os.path.join(result_dir, 'results.json')

    if not os.path.exists(results_file):
        abort(404)

    with open(results_file, 'r') as f:
        results = json.load(f)

    # Check if this result belongs to a batch (for back navigation)
    batch_id = request.args.get('batch')

    return render_template('result.html', r=results, analysis_id=analysis_id, batch_id=batch_id)


@app.route('/result/<analysis_id>/image/<filename>')
def result_image(analysis_id, filename):
    """Serve generated analysis images (ELA, noise, thumbnails, etc.)"""
    validate_id(analysis_id)
    safe_name = secure_filename(filename)
    filepath = os.path.join(RESULTS_DIR, analysis_id, safe_name)
    if not os.path.exists(filepath):
        abort(404)
    return send_file(filepath)


@app.route('/result/<analysis_id>/original')
def result_original(analysis_id):
    """Serve the uploaded original image."""
    validate_id(analysis_id)
    result_dir = os.path.join(RESULTS_DIR, analysis_id)
    results_file = os.path.join(result_dir, 'results.json')
    if not os.path.exists(results_file):
        abort(404)
    with open(results_file, 'r') as f:
        results = json.load(f)
    filepath = os.path.join(UPLOAD_DIR, results['stored_name'])
    if not os.path.exists(filepath):
        abort(404)
    return send_file(filepath)


@app.route('/result/<analysis_id>/json')
def result_json(analysis_id):
    validate_id(analysis_id)
    result_dir = os.path.join(RESULTS_DIR, analysis_id)
    results_file = os.path.join(result_dir, 'results.json')
    if not os.path.exists(results_file):
        abort(404)
    with open(results_file, 'r') as f:
        return jsonify(json.load(f))


@app.route('/examples')
def examples_page():
    if not examples_exist(DATA_DIR):
        generate_all_examples(DATA_DIR)
    exs = load_examples(DATA_DIR)
    return render_template('examples.html', examples=exs)


VALID_EXAMPLE_IDS = {'ela', 'noise', 'ghost'}

@app.route('/examples/image/<example_id>/<filename>')
def example_image(example_id, filename):
    if example_id not in VALID_EXAMPLE_IDS:
        abort(404)
    safe_name = secure_filename(filename)
    examples_dir = get_examples_dir(DATA_DIR)
    filepath = os.path.join(examples_dir, example_id, safe_name)
    if not os.path.exists(filepath):
        abort(404)
    return send_file(filepath)


@app.route('/clear', methods=['POST'])
def clear():
    batch_dir = os.path.join(DATA_DIR, 'batches')
    for d in [UPLOAD_DIR, RESULTS_DIR, batch_dir]:
        if os.path.exists(d):
            shutil.rmtree(d)
        os.makedirs(d, exist_ok=True)
    history_file = os.path.join(DATA_DIR, 'history.json')
    if os.path.exists(history_file):
        os.remove(history_file)
    flash('All analyses cleared.', 'success')
    return redirect(url_for('index'))
