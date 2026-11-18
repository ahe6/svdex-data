#!/usr/bin/env python3
"""Publish a validated latest snapshot; never put dataset payloads in Git history."""
import argparse
import json
from pathlib import Path
import subprocess

from dataset import download_snapshot, read_snapshot

REPOSITORY = 'ahe6/svdex-data'


def gh(*args):
    return subprocess.check_output(['gh', *args], text=True)


def release():
    releases = json.loads(gh('api', f'repos/{REPOSITORY}/releases?per_page=100'))
    return next((item for item in releases if item['tag_name'] == 'latest'), None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['previous', 'publish'])
    parser.add_argument('directory')
    args = parser.parse_args()
    current = release()
    if args.action == 'previous':
        if current and any(a['name'] == 'manifest.json' for a in current['assets']):
            download_snapshot(f'https://github.com/{REPOSITORY}/releases/download/latest', args.directory)
        return
    directory = Path(args.directory)
    manifest, _ = read_snapshot(directory)
    if manifest['source'] != 'https://svdex.com':
        raise ValueError('Only a production guest export can be published here')
    if not current:
        gh('release', 'create', 'latest', '--repo', REPOSITORY, '--title', 'Latest public SVDEX dataset',
           '--notes', 'Daily public-only snapshot. See the repository README for loading, scope, rights and removals.', '--latest')
    filenames = [spec['name'] for spec in manifest['files'].values()] + [manifest['removed']['name']]
    # Versioned payloads first, stable manifest last. Failed uploads leave the
    # previous manifest usable. Readers always validate checksums before loading.
    gh('release', 'upload', 'latest', '--repo', REPOSITORY, *[str(directory / name) for name in filenames])
    gh('release', 'upload', 'latest', '--repo', REPOSITORY, str(directory / 'manifest.json'), '--clobber')
    # Verify the public download independently before removing superseded files.
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        download_snapshot(f'https://github.com/{REPOSITORY}/releases/download/latest', Path(root) / 'verify')
    keep = set(filenames) | {'manifest.json'}
    current = release()
    for asset in current['assets']:
        if asset['name'] not in keep:
            gh('release', 'delete-asset', 'latest', asset['name'], '--repo', REPOSITORY, '--yes')
    print('Published verified snapshot ' + manifest['snapshot_id'])


if __name__ == '__main__':
    main()
