"""
Thumbnail mismatch detection.

JPEG files often contain an embedded thumbnail in the EXIF data.
If someone edits the main image but the thumbnail is not regenerated,
the thumbnail will still show the original unedited image.
This is a reliable and hard-to-fake indicator of manipulation.
"""

import subprocess
import os
import io
import hashlib
import numpy as np
from PIL import Image, ImageOps


def extract_embedded_thumbnail(filepath):
    """Extract the EXIF thumbnail using exiftool."""
    try:
        result = subprocess.run(
            ['exiftool', '-b', '-ThumbnailImage', filepath],
            capture_output=True, timeout=15
        )
        if result.returncode == 0 and result.stdout and len(result.stdout) > 100:
            return result.stdout
    except Exception:
        pass
    return None


def normalize_for_comparison(img, size=(180, 180)):
    """Normalize orientation and aspect ratio before thumbnail comparison."""
    normalized = ImageOps.exif_transpose(img).convert('RGB')
    return ImageOps.fit(normalized, size, Image.LANCZOS, centering=(0.5, 0.5))


def images_differ(img1, img2, threshold=0.25):
    """
    Compare two images for meaningful differences.
    Returns (differs: bool, difference_score: float, diff_image: Image)
    """
    a = normalize_for_comparison(img1)
    b = normalize_for_comparison(img2)

    arr_a = np.array(a, dtype=np.float32) / 255.0
    arr_b = np.array(b, dtype=np.float32) / 255.0

    # Mean absolute difference
    diff = np.abs(arr_a - arr_b)
    score = float(np.mean(diff))

    # Generate visual diff image (amplified)
    diff_amplified = np.clip(diff * 5.0, 0, 1)
    diff_img = Image.fromarray((diff_amplified * 255).astype(np.uint8))

    return score > threshold, score, diff_img


def analyze_thumbnail(filepath, output_dir=None):
    """Check for thumbnail/main image mismatch."""

    try:
        main_img = Image.open(filepath)
    except Exception:
        return {
            'status': 'error',
            'summary': 'Could not open image.',
            'findings': [],
        }

    thumb_data = extract_embedded_thumbnail(filepath)

    if thumb_data is None:
        return {
            'status': 'none',
            'summary': 'No embedded thumbnail — mismatch check skipped.',
            'detail': 'Most camera photos contain a small preview image (thumbnail) embedded in the EXIF data. '
                      'If someone edits the main image but the thumbnail isn\'t regenerated, it still shows the '
                      'original — a reliable sign of tampering. This image has no embedded thumbnail, so that '
                      'comparison can\'t be done. This is normal for screenshots, social media exports, PNGs, '
                      'and images saved by software that strips EXIF data.',
            'findings': [{
                'check': 'Thumbnail presence',
                'result': 'No embedded thumbnail',
                'detail': 'Camera photos usually embed a small preview image in EXIF. This image doesn\'t have one, '
                          'which is common for screenshots, web exports, and non-camera images. '
                          'The thumbnail mismatch check (comparing the preview to the actual image to detect edits) '
                          'cannot be performed.',
                'severity': 'info',
            }],
            'thumb_hash': None,
            'main_hash': None,
        }

    try:
        thumb_img = Image.open(io.BytesIO(thumb_data))
    except Exception:
        return {
            'status': 'error',
            'summary': 'Could not decode embedded thumbnail.',
            'findings': [],
        }

    # Compare
    differs, score, diff_img = images_differ(main_img, thumb_img)

    # Hash both for the record
    thumb_hash = hashlib.sha256(thumb_data).hexdigest()[:16]

    findings = []
    saved_paths = {}

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        # Save thumbnail
        thumb_path = os.path.join(output_dir, 'thumbnail.jpg')
        normalize_for_comparison(thumb_img, size=(300, 300)).save(thumb_path, 'JPEG', quality=95)
        saved_paths['thumbnail'] = thumb_path

        # Save main image resized for comparison
        main_resized = normalize_for_comparison(main_img, size=(300, 300))
        main_path = os.path.join(output_dir, 'main_resized.jpg')
        main_resized.save(main_path, 'JPEG', quality=95)
        saved_paths['main_resized'] = main_path

        # Save diff
        diff_path = os.path.join(output_dir, 'thumb_diff.jpg')
        diff_img.save(diff_path, 'JPEG', quality=95)
        saved_paths['diff'] = diff_path

    if differs:
        findings.append({
            'check': 'Thumbnail mismatch',
            'result': f'MISMATCH DETECTED (difference: {score:.1%})',
            'detail': 'The embedded thumbnail does not match the main image. '
                      'This can happen after editing, but it can also result from legitimate '
                      're-exports, crops, or thumbnail regeneration quirks. '
                      'Treat this as a strong lead that deserves corroboration.',
            'severity': 'suspicious',
        })
        status = 'mismatch'
        summary = f'Thumbnail does not match main image (difference: {score:.1%}). Review with caution.'
    else:
        findings.append({
            'check': 'Thumbnail mismatch',
            'result': f'Thumbnail matches main image (difference: {score:.1%})',
            'detail': 'The embedded thumbnail is broadly consistent with the main image.',
            'severity': 'ok',
        })
        status = 'ok'
        summary = 'Thumbnail matches main image.'

    return {
        'status': status,
        'summary': summary,
        'findings': findings,
        'difference_score': score,
        'thumb_hash': thumb_hash,
        'saved_paths': saved_paths,
    }
