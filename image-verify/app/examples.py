"""
Generate synthetic example images that demonstrate what ELA, noise analysis,
and JPEG ghost detection look like when they detect something real.

These are intentionally crude manipulations — the kind of thing these
techniques were designed to catch (before AI editing made them less reliable).
"""

import os
import io
import numpy as np
from PIL import Image, ImageDraw


def get_examples_dir(data_dir):
    return os.path.join(data_dir, 'examples')


def examples_exist(data_dir):
    return os.path.exists(os.path.join(get_examples_dir(data_dir), 'manifest.json'))


def generate_all_examples(data_dir):
    """Generate all example images and their analyses."""
    import json
    from app.analysis.forensics import analyze_ela, analyze_noise, analyze_jpeg_ghosts

    d = get_examples_dir(data_dir)
    os.makedirs(d, exist_ok=True)

    examples = []

    # ── Example 1: ELA — spliced region from different JPEG quality ──
    ela_dir = os.path.join(d, 'ela')
    os.makedirs(ela_dir, exist_ok=True)

    # Create a "background" — save at quality 60 (heavy compression)
    bg = Image.new('RGB', (600, 400))
    # Draw a gradient background
    pixels = np.zeros((400, 600, 3), dtype=np.uint8)
    for y in range(400):
        for x in range(600):
            pixels[y, x] = [
                int(40 + (x / 600) * 80),
                int(60 + (y / 400) * 60),
                int(100 + ((x + y) / 1000) * 80)
            ]
    bg = Image.fromarray(pixels)
    draw = ImageDraw.Draw(bg)
    # Add some texture — circles and lines
    for i in range(20):
        x = np.random.randint(0, 600)
        y = np.random.randint(0, 400)
        r = np.random.randint(10, 40)
        color = (np.random.randint(30, 120), np.random.randint(40, 130), np.random.randint(80, 180))
        draw.ellipse([x-r, y-r, x+r, y+r], fill=color)

    # Save at quality 60 and reload (bakes in the compression artifacts)
    buf60 = io.BytesIO()
    bg.save(buf60, 'JPEG', quality=60)
    buf60.seek(0)
    bg_compressed = Image.open(buf60).copy()

    # Create a "foreign" patch — bright red rectangle, saved at quality 95
    patch = Image.new('RGB', (150, 100))
    patch_pixels = np.zeros((100, 150, 3), dtype=np.uint8)
    for y in range(100):
        for x in range(150):
            patch_pixels[y, x] = [180 + np.random.randint(-10, 10),
                                   40 + np.random.randint(-10, 10),
                                   40 + np.random.randint(-10, 10)]
    patch = Image.fromarray(patch_pixels)
    buf95 = io.BytesIO()
    patch.save(buf95, 'JPEG', quality=95)
    buf95.seek(0)
    patch_compressed = Image.open(buf95).copy()

    # Paste the high-quality patch into the low-quality background
    composite = bg_compressed.copy()
    composite.paste(patch_compressed, (200, 150))

    # Save the composite at quality 85 (middle ground)
    ela_path = os.path.join(ela_dir, 'spliced.jpg')
    composite.save(ela_path, 'JPEG', quality=85)

    # Run ELA
    ela_result = analyze_ela(ela_path, output_dir=ela_dir)

    examples.append({
        'id': 'ela',
        'title': 'ELA — Spliced Region Detection',
        'description': 'A region compressed at JPEG quality 95 was pasted into a background '
                       'compressed at quality 60, then saved at quality 85. The pasted region '
                       'has a different compression history, so ELA shows it at a different '
                       'error level than the surrounding area.',
        'what_to_look_for': 'The bright rectangle in the ELA image shows the spliced region. '
                            'It appears brighter because its compression artifacts differ from '
                            'the background. In a real investigation, you\'d see a similar glow '
                            'around any copy-pasted element that came from a different source.',
        'caveat': 'Modern AI inpainting (Photoshop Generative Fill, Stable Diffusion) recompresses '
                  'the entire image coherently. The spliced region would NOT show up in ELA because '
                  'the AI generates pixels that match the surrounding compression pattern.',
        'image': 'spliced.jpg',
        'analysis_image': 'ela.jpg' if ela_result.get('saved_paths', {}).get('ela') else None,
        'stats': ela_result.get('stats', {}),
    })

    # ── Example 2: Noise — composited regions with different noise ──
    noise_dir = os.path.join(d, 'noise')
    os.makedirs(noise_dir, exist_ok=True)

    # Create two halves with very different noise characteristics
    left_half = np.zeros((400, 300, 3), dtype=np.uint8)
    right_half = np.zeros((400, 300, 3), dtype=np.uint8)

    # Left: smooth gradient with low noise (like a clean studio shot)
    for y in range(400):
        for x in range(300):
            base = [80 + int(x * 0.2), 100 + int(y * 0.1), 140]
            noise = np.random.randint(-3, 4, 3)  # very low noise
            left_half[y, x] = np.clip(np.array(base) + noise, 0, 255)

    # Right: similar gradient but with heavy noise (like a high-ISO phone shot)
    for y in range(400):
        for x in range(300):
            base = [80 + int(x * 0.2), 100 + int(y * 0.1), 140]
            noise = np.random.randint(-25, 26, 3)  # heavy noise
            right_half[y, x] = np.clip(np.array(base) + noise, 0, 255)

    # Combine
    noise_composite = np.concatenate([left_half, right_half], axis=1)
    noise_img = Image.fromarray(noise_composite)
    noise_path = os.path.join(noise_dir, 'noise_mismatch.jpg')
    noise_img.save(noise_path, 'JPEG', quality=92)

    noise_result = analyze_noise(noise_path, output_dir=noise_dir)

    examples.append({
        'id': 'noise',
        'title': 'Noise Analysis — Mismatched Noise Patterns',
        'description': 'The left half has very low noise (like a clean, well-lit photo) while '
                       'the right half has heavy noise (like a high-ISO shot in low light). '
                       'If these came from the same camera at the same settings, the noise would '
                       'be uniform. The mismatch suggests compositing from two different sources.',
        'what_to_look_for': 'The noise map shows a clear difference between the left and right halves. '
                            'The right side has visibly more texture. In a real investigation, you\'d see '
                            'similar inconsistencies where a region was pasted from a different photo.',
        'caveat': 'AI editing tools synthesize noise that matches the surrounding context. '
                  'A modern inpainted region would have consistent noise with its neighbors. '
                  'Also, natural images can have noise variation from lighting differences.',
        'image': 'noise_mismatch.jpg',
        'analysis_image': 'noise.jpg' if noise_result.get('saved_paths', {}).get('noise') else None,
        'stats': noise_result.get('stats', {}),
    })

    # ── Example 3: JPEG Ghosts — double-compressed regions ──
    ghost_dir = os.path.join(d, 'ghost')
    os.makedirs(ghost_dir, exist_ok=True)

    # Create a scene, save at quality 70
    scene = np.zeros((400, 600, 3), dtype=np.uint8)
    for y in range(400):
        for x in range(600):
            scene[y, x] = [
                int(120 + 40 * np.sin(x / 30)),
                int(100 + 30 * np.cos(y / 25)),
                int(80 + 20 * np.sin((x + y) / 40))
            ]
    scene_img = Image.fromarray(scene)
    buf70 = io.BytesIO()
    scene_img.save(buf70, 'JPEG', quality=70)
    buf70.seek(0)
    scene_q70 = Image.open(buf70).copy()

    # Create a different element, save at quality 95
    element = np.zeros((120, 180, 3), dtype=np.uint8)
    for y in range(120):
        for x in range(180):
            element[y, x] = [
                int(200 + 20 * np.sin(x / 10)),
                int(180 + 15 * np.cos(y / 8)),
                int(50 + 30 * np.sin((x + y) / 15))
            ]
    element_img = Image.fromarray(element)
    buf95e = io.BytesIO()
    element_img.save(buf95e, 'JPEG', quality=95)
    buf95e.seek(0)
    element_q95 = Image.open(buf95e).copy()

    # Paste q95 element into q70 scene
    ghost_composite = scene_q70.copy()
    ghost_composite.paste(element_q95, (210, 140))

    # Save final at quality 80
    ghost_path = os.path.join(ghost_dir, 'ghost_composite.jpg')
    ghost_composite.save(ghost_path, 'JPEG', quality=80)

    ghost_result = analyze_jpeg_ghosts(ghost_path, output_dir=ghost_dir)

    examples.append({
        'id': 'ghost',
        'title': 'JPEG Ghosts — Double Compression Detection',
        'description': 'The background was originally compressed at JPEG quality 70. A separate element '
                       'compressed at quality 95 was pasted in, and the result saved at quality 80. '
                       'Each region now has a different "native" quality level — the ghost detection '
                       'maps which quality level each region was originally compressed at.',
        'what_to_look_for': 'The color map shows different regions in different colors, indicating '
                            'they were originally compressed at different JPEG quality levels. '
                            'Uniform color = single compression history. Multiple colors = possible compositing.',
        'caveat': 'This only works on JPEGs, and flat-color regions or sharp edges can produce '
                  'false positives. AI-generated edits typically pass this test because the AI '
                  'doesn\'t introduce double-compression artifacts.',
        'image': 'ghost_composite.jpg',
        'analysis_image': 'jpeg_ghosts.jpg' if ghost_result.get('saved_paths', {}).get('jpeg_ghosts') else None,
        'stats': ghost_result.get('stats', {}),
    })

    # Save manifest
    with open(os.path.join(d, 'manifest.json'), 'w') as f:
        import json
        json.dump(examples, f, indent=2)

    return examples


def load_examples(data_dir):
    """Load existing examples."""
    import json
    manifest = os.path.join(get_examples_dir(data_dir), 'manifest.json')
    if os.path.exists(manifest):
        with open(manifest) as f:
            return json.load(f)
    return []
