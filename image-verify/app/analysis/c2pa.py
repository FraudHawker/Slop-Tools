"""
C2PA (Coalition for Content Provenance and Authenticity) verification.

Checks for cryptographic provenance signatures embedded in images.
If present, verifies the signature chain and reports the provenance history.

C2PA is the gold standard — it's cryptographic, not heuristic.
A valid C2PA signature means the image has an unbroken chain of custody
from the capture device through any edits.
"""

import json

# c2pa-python may not be available on all platforms
try:
    import c2pa
    C2PA_AVAILABLE = True
except ImportError:
    C2PA_AVAILABLE = False


def analyze_c2pa(filepath):
    """Check for C2PA content credentials."""

    if not C2PA_AVAILABLE:
        return {
            'status': 'unavailable',
            'summary': 'C2PA library not available on this platform.',
            'detail': 'The c2pa-python package could not be installed. '
                      'C2PA verification is disabled.',
            'findings': [],
            'provenance': None,
        }

    try:
        # Detect MIME type
        mime_type = 'image/jpeg'  # default
        ext = filepath.lower().rsplit('.', 1)[-1] if '.' in filepath else ''
        mime_map = {
            'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
            'png': 'image/png', 'tiff': 'image/tiff', 'tif': 'image/tiff',
            'webp': 'image/webp', 'heic': 'image/heic', 'heif': 'image/heif',
            'avif': 'image/avif',
        }
        mime_type = mime_map.get(ext, mime_type)

        # Read the manifest store from the file
        with open(filepath, 'rb') as f:
            reader = c2pa.Reader(mime_type, f)
        manifest_store = json.loads(reader.json())

        if not manifest_store:
            return {
                'status': 'none',
                'summary': 'No C2PA provenance data found.',
                'detail': 'This image does not contain C2PA content credentials. '
                          'Most images currently do not contain C2PA content credentials. '
                          'Adoption is still limited to select workflows and select cameras '
                          'from manufacturers such as Leica, Nikon, and Sony. '
                          'Absence of C2PA does not indicate tampering.',
                'findings': [],
                'provenance': None,
            }

        # Parse the manifest
        findings = []
        manifests = manifest_store.get('manifests', {})
        active_manifest = manifest_store.get('active_manifest', '')

        # Extract provenance chain
        provenance_chain = []
        for manifest_id, manifest in manifests.items():
            claim = manifest.get('claim_generator', 'Unknown')
            title = manifest.get('title', 'Unknown')
            assertions = manifest.get('assertions', [])

            actions = []
            for assertion in assertions:
                label = assertion.get('label', '')
                if 'actions' in label.lower():
                    action_data = assertion.get('data', {})
                    for action in action_data.get('actions', []):
                        actions.append(action.get('action', 'unknown'))

            provenance_chain.append({
                'manifest_id': manifest_id,
                'generator': claim,
                'title': title,
                'actions': actions,
                'is_active': manifest_id == active_manifest,
            })

        # Check validation status
        validation_status = manifest_store.get('validation_status', [])
        has_errors = any(s.get('code', '').startswith('error') for s in validation_status)

        if has_errors:
            findings.append({
                'check': 'C2PA signature',
                'result': 'C2PA present but validation FAILED',
                'detail': 'Content credentials exist but the signature chain is broken or invalid. '
                          'The image may have been modified after signing.',
                'severity': 'concerning',
            })
            status = 'invalid'
            summary = 'C2PA credentials present but signature validation failed.'
        else:
            findings.append({
                'check': 'C2PA signature',
                'result': 'Valid C2PA provenance chain',
                'detail': f'Image has a verified provenance chain with {len(provenance_chain)} '
                          f'manifest(s). This is a cryptographic guarantee that the provenance chain is intact.',
                'severity': 'verified',
            })
            status = 'verified'
            summary = 'Image has valid C2PA content credentials with an intact provenance chain.'

        return {
            'status': status,
            'summary': summary,
            'findings': findings,
            'provenance': provenance_chain,
            'validation_errors': validation_status if has_errors else [],
            'raw_manifest': manifest_store,
        }

    except (c2pa.C2paError if C2PA_AVAILABLE else Exception) as e:
        error_str = str(e).lower()
        if 'manifest' in error_str or 'not found' in error_str or 'jumbf' in error_str:
            return {
                'status': 'none',
                'summary': 'No C2PA provenance data found.',
                'detail': 'This image does not contain C2PA content credentials. '
                          'Most images currently do not contain C2PA content credentials. '
                          'Adoption is still limited to select workflows and select cameras '
                          'from manufacturers such as Leica, Nikon, and Sony. '
                          'Absence of C2PA does not indicate tampering.',
                'findings': [],
                'provenance': None,
            }

        return {
            'status': 'error',
            'summary': f'C2PA check encountered an error: {str(e)[:200]}',
            'findings': [],
            'provenance': None,
        }
