"""
Image forensics: ELA, noise analysis, JPEG ghost detection.

IMPORTANT CAVEATS — these techniques have real limitations:

ELA (Error Level Analysis):
  Detects regions with different JPEG compression levels, which can indicate
  copy-paste edits. HOWEVER, modern AI inpainting (Photoshop Generative Fill,
  Stable Diffusion) recompresses coherently and often produces clean ELA.
  A clean ELA does NOT mean the image is unedited.

Noise Analysis:
  Different cameras and editing operations produce different noise patterns.
  Spliced regions from different sources may have mismatched noise.
  HOWEVER, AI tools can synthesize matching noise patterns.

JPEG Ghost Detection:
  Resaved JPEGs at different quality levels leave "ghosts" — regions where
  the error pattern differs because they were compressed at a different quality.
  HOWEVER, this only works on JPEGs and is ineffective against AI edits.

Bottom line: these are supplementary signals, not proof. Use alongside
metadata analysis, C2PA, and thumbnail checks.
"""

import os
import io
import numpy as np
from PIL import Image
import cv2


def analyze_ela(filepath, quality=90, output_dir=None):
    """
    Error Level Analysis.
    Resaves at a known quality and diffs against original.
    Edited regions may show different error levels.
    """
    try:
        original = Image.open(filepath).convert('RGB')
    except Exception:
        return {
            'status': 'error',
            'summary': 'Could not open image.',
            'findings': [],
        }

    # Resave at known quality
    buffer = io.BytesIO()
    original.save(buffer, 'JPEG', quality=quality)
    buffer.seek(0)
    resaved = Image.open(buffer).convert('RGB')

    # Compute difference
    orig_arr = np.array(original, dtype=np.float32)
    resaved_arr = np.array(resaved, dtype=np.float32)
    diff = np.abs(orig_arr - resaved_arr)

    # Scale up for visibility
    scale = 255.0 / (diff.max() + 1e-6)
    ela_image = np.clip(diff * scale, 0, 255).astype(np.uint8)
    ela_pil = Image.fromarray(ela_image)

    # Analyze variance across the image
    # Split into grid and check for outlier regions
    h, w = diff.shape[:2]
    grid_h, grid_w = max(1, h // 8), max(1, w // 8)
    region_means = []
    for y in range(0, h - grid_h + 1, grid_h):
        for x in range(0, w - grid_w + 1, grid_w):
            region = diff[y:y+grid_h, x:x+grid_w]
            region_means.append(float(np.mean(region)))

    if region_means:
        overall_mean = np.mean(region_means)
        overall_std = np.std(region_means)
        max_region = max(region_means)
        # Outlier detection: any region > 2 std devs from mean
        outliers = sum(1 for m in region_means if m > overall_mean + 2 * overall_std)
        outlier_ratio = outliers / len(region_means)
    else:
        overall_mean = 0
        overall_std = 0
        outlier_ratio = 0
        max_region = 0

    findings = []

    if outlier_ratio > 0.05 and overall_std > 3:
        findings.append({
            'check': 'Error Level Analysis',
            'result': f'Uneven error levels detected ({outlier_ratio:.0%} outlier regions)',
            'detail': 'Some regions show significantly different compression artifacts than others. '
                      'This CAN indicate editing, but has significant limitations — see caveats.',
            'severity': 'note',
            'caveat': 'ELA is unreliable against modern AI editing tools which recompress coherently. '
                      'A suspicious ELA result is a lead, not proof.',
        })
    else:
        findings.append({
            'check': 'Error Level Analysis',
            'result': 'Relatively uniform error levels',
            'detail': 'Error levels are fairly consistent across the image.',
            'severity': 'ok',
            'caveat': 'A clean ELA does NOT mean the image is authentic. '
                      'Modern AI edits produce clean, uniform ELA results.',
        })

    saved_paths = {}
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        ela_path = os.path.join(output_dir, 'ela.jpg')
        ela_pil.save(ela_path, 'JPEG', quality=95)
        saved_paths['ela'] = ela_path

    return {
        'status': 'note' if outlier_ratio > 0.05 else 'ok',
        'summary': f'ELA: {"uneven" if outlier_ratio > 0.05 else "uniform"} error levels '
                   f'(mean: {overall_mean:.1f}, std: {overall_std:.1f})',
        'findings': findings,
        'stats': {
            'mean_error': float(overall_mean),
            'std_error': float(overall_std),
            'outlier_ratio': float(outlier_ratio),
            'max_region_error': float(max_region),
        },
        'saved_paths': saved_paths,
    }


def analyze_noise(filepath, output_dir=None):
    """
    Noise pattern analysis.
    Extracts the noise layer and checks for inconsistencies.
    """
    try:
        img = cv2.imread(filepath)
        if img is None:
            raise ValueError("Could not read image")
    except Exception:
        return {
            'status': 'error',
            'summary': 'Could not open image.',
            'findings': [],
        }

    # Convert to grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Extract noise by subtracting a blurred version
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    noise = cv2.absdiff(gray, blurred)

    # Amplify for visibility
    noise_amplified = cv2.normalize(noise, None, 0, 255, cv2.NORM_MINMAX)

    # Analyze noise consistency across grid
    h, w = noise.shape
    grid_h, grid_w = max(1, h // 8), max(1, w // 8)
    region_stds = []
    for y in range(0, h - grid_h + 1, grid_h):
        for x in range(0, w - grid_w + 1, grid_w):
            region = noise[y:y+grid_h, x:x+grid_w]
            region_stds.append(float(np.std(region)))

    findings = []

    if region_stds:
        overall_std = np.std(region_stds)
        overall_mean = np.mean(region_stds)
        cv_noise = overall_std / (overall_mean + 1e-6)  # coefficient of variation

        if cv_noise > 0.5:
            findings.append({
                'check': 'Noise analysis',
                'result': f'Inconsistent noise patterns (CV: {cv_noise:.2f})',
                'detail': 'Noise levels vary significantly across the image. '
                          'This CAN indicate compositing from different sources.',
                'severity': 'note',
                'caveat': 'Noise inconsistency can also result from JPEG compression, '
                          'different lighting conditions, or image resizing. '
                          'AI editing tools can synthesize matching noise.',
            })
        else:
            findings.append({
                'check': 'Noise analysis',
                'result': f'Consistent noise pattern (CV: {cv_noise:.2f})',
                'detail': 'Noise levels are relatively uniform across the image.',
                'severity': 'ok',
                'caveat': 'Consistent noise does NOT prove authenticity. '
                          'Modern AI tools produce coherent noise patterns.',
            })
    else:
        cv_noise = 0

    saved_paths = {}
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        noise_path = os.path.join(output_dir, 'noise.jpg')
        cv2.imwrite(noise_path, noise_amplified)
        saved_paths['noise'] = noise_path

    return {
        'status': 'note' if cv_noise > 0.5 else 'ok',
        'summary': f'Noise: {"inconsistent" if cv_noise > 0.5 else "consistent"} '
                   f'(CV: {cv_noise:.2f})',
        'findings': findings,
        'stats': {
            'noise_cv': float(cv_noise),
            'mean_noise_std': float(overall_mean) if region_stds else 0,
        },
        'saved_paths': saved_paths,
    }


def analyze_jpeg_ghosts(filepath, output_dir=None):
    """
    JPEG ghost detection.
    Resaves at multiple quality levels and looks for regions where
    the error changes non-uniformly — indicating prior compression
    at a different quality level.
    """
    try:
        original = Image.open(filepath).convert('RGB')
    except Exception:
        return {
            'status': 'error',
            'summary': 'Could not open image.',
            'findings': [],
        }

    orig_arr = np.array(original, dtype=np.float32)
    qualities = [60, 70, 80, 90, 95]
    ghost_maps = []

    for q in qualities:
        buf = io.BytesIO()
        original.save(buf, 'JPEG', quality=q)
        buf.seek(0)
        resaved = np.array(Image.open(buf).convert('RGB'), dtype=np.float32)
        diff = np.mean(np.abs(orig_arr - resaved), axis=2)
        ghost_maps.append((q, diff))

    # Find the quality level where some regions have minimum error
    # (indicating prior compression at that quality)
    min_error_map = np.full(orig_arr.shape[:2], 999.0)
    min_quality_map = np.zeros(orig_arr.shape[:2], dtype=np.int32)

    for q, diff in ghost_maps:
        mask = diff < min_error_map
        min_error_map = np.where(mask, diff, min_error_map)
        min_quality_map = np.where(mask, q, min_quality_map)

    # Check if different regions of the image have different "best" qualities
    h, w = min_quality_map.shape
    grid_h, grid_w = max(1, h // 6), max(1, w // 6)
    region_qualities = []
    for y in range(0, h - grid_h + 1, grid_h):
        for x in range(0, w - grid_w + 1, grid_w):
            region = min_quality_map[y:y+grid_h, x:x+grid_w]
            region_qualities.append(int(np.median(region)))

    findings = []
    unique_qualities = len(set(region_qualities))

    if unique_qualities >= 3 and len(region_qualities) > 4:
        findings.append({
            'check': 'JPEG ghost detection',
            'result': f'Multiple compression levels detected ({unique_qualities} distinct)',
            'detail': 'Different regions appear to have been compressed at different JPEG quality levels. '
                      'This CAN indicate that parts of the image were composited from different sources.',
            'severity': 'note',
            'caveat': 'JPEG ghost analysis has limited reliability. Regions with flat colors or '
                      'sharp edges can produce false positives. AI edits often pass this check.',
        })
    else:
        findings.append({
            'check': 'JPEG ghost detection',
            'result': 'Consistent compression across image',
            'detail': 'The image shows a uniform JPEG compression history.',
            'severity': 'ok',
            'caveat': 'A clean result does NOT prove authenticity.',
        })

    # Generate visualization — show the min-quality map
    saved_paths = {}
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        # Normalize quality map to 0-255 for visualization
        q_vis = ((min_quality_map - 60) / 35.0 * 255).clip(0, 255).astype(np.uint8)
        q_vis_colored = cv2.applyColorMap(q_vis, cv2.COLORMAP_JET)
        ghost_path = os.path.join(output_dir, 'jpeg_ghosts.jpg')
        cv2.imwrite(ghost_path, q_vis_colored)
        saved_paths['jpeg_ghosts'] = ghost_path

    return {
        'status': 'note' if unique_qualities >= 3 else 'ok',
        'summary': f'JPEG ghosts: {unique_qualities} compression level(s) detected',
        'findings': findings,
        'saved_paths': saved_paths,
    }
