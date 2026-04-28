"""
Reverse image search integration.

Generates search URLs for major reverse image search engines.
Does NOT scrape results (that breaks TOS and is fragile).
Instead, provides one-click links the user can open.

This is deliberately simple — reverse image search APIs are either
expensive (TinEye), rate-limited, or require scraping (Google).
Better to give the user direct links than build something brittle.
"""

import urllib.parse


def get_search_links(filepath, public_url=None):
    """
    Generate reverse image search URLs.

    If public_url is provided (the image is accessible via URL),
    uses URL-based search. Otherwise, provides upload page links.
    """
    links = []

    if public_url:
        # URL-based searches (more reliable)
        encoded_url = urllib.parse.quote(public_url, safe='')

        links.append({
            'engine': 'Google Lens',
            'url': f'https://lens.google.com/uploadbyurl?url={encoded_url}',
            'type': 'url',
        })

        links.append({
            'engine': 'TinEye',
            'url': f'https://tineye.com/search?url={encoded_url}',
            'type': 'url',
        })

        links.append({
            'engine': 'Yandex',
            'url': f'https://yandex.com/images/search?url={encoded_url}&rpt=imageview',
            'type': 'url',
        })

        links.append({
            'engine': 'Bing Visual Search',
            'url': f'https://www.bing.com/images/search?view=detailv2&iss=sbi&form=SBIIDP&q=imgurl:{encoded_url}',
            'type': 'url',
        })

    else:
        # Upload-based searches (user must upload manually)
        links.append({
            'engine': 'Google Lens',
            'url': 'https://lens.google.com/',
            'type': 'upload',
        })

        links.append({
            'engine': 'TinEye',
            'url': 'https://tineye.com/',
            'type': 'upload',
        })

        links.append({
            'engine': 'Yandex Images',
            'url': 'https://yandex.com/images/',
            'type': 'upload',
        })

        links.append({
            'engine': 'Bing Visual Search',
            'url': 'https://www.bing.com/visualsearch',
            'type': 'upload',
        })

    return {
        'status': 'ready',
        'summary': f'{len(links)} reverse image search engines available.',
        'links': links,
        'has_url': public_url is not None,
    }
