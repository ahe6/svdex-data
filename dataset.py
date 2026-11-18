#!/usr/bin/env python3
"""Guest-only SVDEX exports and a credential-free local SQLite research loader."""
import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = 1
TABLES = ('categories', 'tags', 'topics', 'posts', 'companies', 'funds', 'people', 'authors')
MAX_BYTES = 128 * 1024 * 1024
TOPIC_FIELDS = 'id category_id title slug created_at last_posted_at user_id reply_count views like_count closed archived'.split()
POST_FIELDS = 'id topic_id user_id username name created_at updated_at post_number post_type action_code reply_to_post_number cooked'.split()
LISTING_FIELDS = 'instrument_type summary terms valuation_cap conversion_discount strike_price expiration interest_rate offering_size funds_raised total_funding minimum post_money_valuation_usd company_tag_ids'.split()
COMPANY_FIELDS = 'entity_type strategy_focus manager_name manager_url aum_usd fund_size_usd fund_size_basis fund_life vintage_year fund_domicile facts_as_of source_url kind total_funding_usd last_valuation_usd stage headquarters_country offerings open_offering open_offerings'.split()
PERSON_FIELDS = 'background role focus location'.split()


def pick(obj, fields):
    """Every outgoing object is constructed with an explicit field allowlist."""
    return {key: obj[key] for key in fields if key in obj and (obj[key] is None or isinstance(obj[key], (str, int, float, bool)))}


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc) or new.username or new.password or not new.path.startswith(('/c/', '/t/', '/categories', '/tags', '/listings/', '/people/')):
            raise ValueError('Redirect outside public API refused')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GuestClient:
    def __init__(self, base='https://svdex.com', delay=0.4):
        parsed = urllib.parse.urlsplit(base)
        local = parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')
        if (parsed.scheme != 'https' and not local) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('Use an HTTPS origin without credentials, path or query')
        self.base = base.rstrip('/')
        self.delay = delay
        # No cookie jar, proxy credentials, environment API keys or login support.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirect())
        self.last_request = 0

    def get(self, path):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('Only relative guest API paths are allowed')
        for attempt in range(4):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            req = urllib.request.Request(self.base + path, headers={
                'Accept': 'application/json', 'User-Agent': 'SVDEX-Public-Dataset/1.0',
                'Cache-Control': 'no-cache',
            })
            try:
                with self.opener.open(req, timeout=45) as response:
                    if 'application/json' not in response.headers.get('Content-Type', ''):
                        raise ValueError('Expected public JSON response')
                    body = response.read(MAX_BYTES + 1)
                    if len(body) > MAX_BYTES:
                        raise ValueError('Response exceeds safety limit')
                    result = json.loads(body)
                    if not isinstance(result, dict) or result.get('errors'):
                        raise ValueError('Guest API returned an error')
                    return result
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise
                time.sleep(min(60, max(2 ** attempt, int(error.headers.get('Retry-After', '0')) if error.headers.get('Retry-After', '').isdigit() else 0)))
        raise RuntimeError('Guest request failed')


def tag_record(tag):
    if not isinstance(tag, dict) or not isinstance(tag.get('id'), int) or tag.get('pm_only'):
        return None
    return pick(tag, ('id', 'name', 'slug', 'description'))


def topic_record(topic, category_ids, base):
    if topic.get('archetype') != 'regular' or topic.get('visible') is not True or topic.get('deleted_at') or topic.get('category_id') not in category_ids:
        return None
    record = pick(topic, TOPIC_FIELDS)
    record['url'] = f'{base}/t/{topic["id"]}'
    record['tag_ids'] = sorted(t['id'] for t in topic.get('tags', []) if tag_record(t))
    for key, fields in (
        ('svdex_listing', LISTING_FIELDS),
        ('svdex_presale', ('summary', 'price', 'includes', 'delivery', 'terms')),
        ('svdex_product', ('summary',)),
    ):
        if isinstance(topic.get(key), dict):
            record[key.removeprefix('svdex_')] = pick(topic[key], fields)
            if key == 'svdex_listing':
                record['listing']['company_tag_ids'] = [i for i in topic[key].get('company_tag_ids', []) if type(i) is int]
    return record


def post_record(post, topic_id, base):
    if post.get('topic_id') != topic_id or post.get('post_type') not in (1, 2, 3) or post.get('hidden') or post.get('deleted_at') or post.get('user_deleted'):
        return None
    record = pick(post, POST_FIELDS)
    record['url'] = f'{base}/t/{topic_id}/{post["post_number"]}'
    return record


def entity_record(user, kind, base):
    detail_key = 'svdex_person' if kind == 'people' else 'svdex_company'
    details = user.get(detail_key)
    if not isinstance(details, dict):
        return None  # Core can serialize identities even when profile facts are hidden.
    if kind != 'people' and details.get('kind') != kind:
        return None
    record = pick(user, ('id', 'username', 'name', 'avatar_template'))
    record['url'] = f'{base}/u/{urllib.parse.quote(user["username"], safe="")}/summary'
    summary = user.get('svdex_company_summary') if kind != 'people' else details.get('background')
    record['summary'] = summary if isinstance(summary, str) else None
    record.update(pick(details, PERSON_FIELDS if kind == 'people' else COMPANY_FIELDS))
    for key in ('sector', 'company_tag'):
        if isinstance(details.get(key), dict):
            record[key] = tag_record(details[key])
    for key in ('key_people', 'related_entities', 'organizations'):
        if isinstance(details.get(key), list):
            record[key] = [pick(entry, ('id', 'user_id', 'name', 'username', 'role', 'url')) for entry in details[key] if isinstance(entry, dict)]
    if kind == 'people' and isinstance(details.get('net_worth'), dict):
        record['net_worth'] = pick(details['net_worth'], ('value', 'source', 'date', 'basis'))
    # Interest/bid records and aggregates are deliberately outside dataset v1.
    return record


def collect(client, max_pages=10000):
    rows = {table: {} for table in TABLES}
    description_topics = set()
    category_list = client.get('/categories.json?include_subcategories=true')['category_list']['categories']

    def add_categories(categories):
        for category in categories:
            if category.get('read_restricted') is not False:
                continue
            record = pick(category, ('id', 'name', 'slug', 'parent_category_id', 'description_text', 'color'))
            record['url'] = f'{client.base}/c/{category["id"]}'
            rows['categories'][record['id']] = record
            topic_url = category.get('topic_url', '')
            match = re.fullmatch(r'/t/[^/?#]+/(\d+)', topic_url)
            if match:
                description_topics.add(int(match[1]))
            add_categories(category.get('subcategory_list', []))

    add_categories(category_list)
    tags_response = client.get('/tags.json')
    for tag in tags_response.get('tags', []):
        if record := tag_record(tag):
            rows['tags'][record['id']] = record
    for group in tags_response.get('extras', {}).get('tag_groups', []):
        for tag in group.get('tags', []):
            if record := tag_record(tag):
                record['group'] = group['name']
                rows['tags'][record['id']] = record

    listed_topics = {}
    for category_id in sorted(rows['categories']):
        seen = set()
        for page in range(max_pages):
            data = client.get(f'/c/{category_id}/l/latest.json?page={page}')['topic_list']
            topics = data['topics']
            fresh = {t['id'] for t in topics} - seen
            if topics and not fresh:
                raise ValueError('Category pagination repeated a page')
            seen.update(fresh)
            for topic in topics:
                if topic_record(topic, rows['categories'], client.base):
                    listed_topics[topic['id']] = topic
            if not data.get('more_topics_url'):
                break
        else:
            raise ValueError('Category pagination exceeded limit; refusing a partial export')

    # Some SVDEX lists omit native category-description topics for presentation.
    # Their public category metadata still explicitly links these public pages.
    for topic_id in description_topics:
        listed_topics.setdefault(topic_id, {})
    for topic_id, listed in sorted(listed_topics.items()):
        topic = client.get(f'/t/{topic_id}.json')
        record = topic_record(topic, rows['categories'], client.base)
        if not record:
            continue
        # Products currently add summary only on the list serializer.
        if isinstance(listed.get('svdex_product'), dict):
            record['product'] = pick(listed['svdex_product'], ('summary',))
        stream = topic['post_stream']
        posts = {p['id']: p for p in stream['posts']}
        missing = sorted(set(stream['stream']) - posts.keys())
        for offset in range(0, len(missing), 20):
            query = urllib.parse.urlencode([('post_ids[]', str(i)) for i in missing[offset:offset + 20]])
            more = client.get(f'/t/{topic_id}/posts.json?{query}')
            for post in more['post_stream']['posts']:
                posts[post['id']] = post
        if set(stream['stream']) - posts.keys():
            raise ValueError('Incomplete post stream; refusing a partial export')
        rows['topics'][topic_id] = record
        for tag in topic.get('tags', []):
            if tag_row := tag_record(tag):
                rows['tags'].setdefault(tag_row['id'], tag_row)
        for post in posts.values():
            if post['id'] not in stream['stream']:
                raise ValueError('Unexpected post outside topic stream')
            if post_row := post_record(post, topic_id, client.base):
                rows['posts'][post_row['id']] = post_row
                if isinstance(post.get('user_id'), int):
                    rows['authors'][post['user_id']] = {'id': post['user_id'], **pick(post, ('username', 'name', 'avatar_template'))}

    for kind in ('companies', 'funds', 'people'):
        seen = set()
        path = '/people/directory_items.json?' if kind == 'people' else f'/listings/company-directory.json?kind={kind}&'
        for page in range(max_pages):
            data = client.get(f'{path}page={page}&limit=50')
            items = data['directory_items']
            fresh = {i['user']['id'] for i in items} - seen
            if items and not fresh:
                raise ValueError('Directory pagination repeated a page')
            seen.update(fresh)
            for item in items:
                if record := entity_record(item['user'], kind, client.base):
                    rows[kind][record['id']] = record
            if len(seen) >= data['meta']['total_rows_directory_items']:
                break
            if not items:
                raise ValueError('Directory ended before declared total')
        else:
            raise ValueError('Directory pagination exceeded limit')
    return {name: sorted(records.values(), key=lambda row: row['id']) for name, records in rows.items()}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def write_snapshot(rows, output, source, started_at, previous=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    generated_at = utcnow()
    snapshot_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    manifest = {'schema_version': VERSION, 'source': source, 'started_at': started_at, 'generated_at': generated_at,
                'snapshot_id': snapshot_id, 'visibility': 'logged-out-public', 'files': {},
                'content_rights': 'Original rights holders retain their rights. Code license does not license dataset content.'}
    previous_manifest, prior_rows = read_snapshot(previous) if previous else (None, {name: [] for name in TABLES})
    if previous_manifest and previous_manifest['source'] != source:
        raise ValueError('Previous snapshot is from a different site')
    removed = {name: sorted({r['id'] for r in prior_rows[name]} - {r['id'] for r in rows[name]}) for name in TABLES}
    # A consumer that misses a daily refresh should replace local tables in full.
    for name in TABLES:
        body = b''.join(encoded(row) for row in rows[name])
        payload = gzip.compress(body, mtime=0)
        filename = f'{name}.{snapshot_id}.jsonl.gz'
        (output / filename).write_bytes(payload)
        manifest['files'][name] = {'name': filename, 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload), 'rows': len(rows[name])}
    removed_name = f'removed.{snapshot_id}.json'
    payload = encoded({'compared_to': previous_manifest['snapshot_id'] if previous else None, 'removed_ids': removed})
    (output / removed_name).write_bytes(payload)
    manifest['removed'] = {'name': removed_name, 'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload)}
    (output / 'manifest.json').write_bytes(encoded(manifest))
    read_snapshot(output)
    return manifest


def checked_file(root, spec):
    name = spec['name']
    if not re.fullmatch(r'[a-z]+\.[A-Za-z0-9]+\.(?:jsonl\.gz|json)', name):
        raise ValueError('Unsafe dataset filename')
    path = Path(root) / name
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Unsafe or oversized dataset file')
    body = path.read_bytes()
    if len(body) != spec['bytes'] or hashlib.sha256(body).hexdigest() != spec['sha256']:
        raise ValueError('Dataset checksum mismatch')
    return body


def read_snapshot(root):
    manifest = json.loads((Path(root) / 'manifest.json').read_text())
    if manifest.get('schema_version') != VERSION or manifest.get('visibility') != 'logged-out-public' or set(manifest['files']) != set(TABLES):
        raise ValueError('Unsupported or incomplete dataset schema')
    rows = {}
    for name, spec in manifest['files'].items():
        payload = checked_file(root, spec)
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
            body = stream.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError('Uncompressed data exceeds safety limit')
        records = [json.loads(line) for line in body.splitlines()]
        ids = [record.get('id') for record in records]
        if len(records) != spec['rows'] or any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
            raise ValueError('Invalid dataset record identities/count')
        rows[name] = records
    checked_file(root, manifest['removed'])
    category_ids = {r['id'] for r in rows['categories']}
    topic_ids = {r['id'] for r in rows['topics']}
    if any(t.get('category_id') not in category_ids for t in rows['topics']) or any(p.get('topic_id') not in topic_ids for p in rows['posts']):
        raise ValueError('Broken topic/post relationships')
    return manifest, rows


def load_sqlite(root, database):
    manifest, rows = read_snapshot(root)  # Validate before touching destination.
    destination = Path(database)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.svdex-load-', dir=destination.parent)
    os.close(handle)
    try:
        with sqlite3.connect(temporary) as connection:
            for table in TABLES:
                connection.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY, data TEXT NOT NULL CHECK(json_valid(data)))')
                connection.executemany(f'INSERT INTO {table} VALUES (?, ?)', [(row['id'], encoded(row).decode().strip()) for row in rows[table]])
            connection.execute('CREATE TABLE snapshot (manifest TEXT NOT NULL)')
            connection.execute('INSERT INTO snapshot VALUES (?)', (json.dumps(manifest),))
            connection.execute("CREATE VIEW offerings AS SELECT id, json_extract(data, '$.title') AS title, json_extract(data, '$.listing') AS terms, data FROM topics WHERE json_type(data, '$.listing') = 'object'")
            connection.execute("CREATE VIEW products AS SELECT id, json_extract(data, '$.title') AS title, data FROM topics WHERE json_type(data, '$.product') = 'object' OR json_type(data, '$.presale') = 'object'")
            connection.execute("CREATE VIEW replies AS SELECT id, json_extract(data, '$.topic_id') AS topic_id, json_extract(data, '$.reply_to_post_number') AS reply_to_post_number, data FROM posts WHERE json_extract(data, '$.post_number') > 1")
            connection.execute("CREATE INDEX posts_topic ON posts(json_extract(data, '$.topic_id'))")
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def download_snapshot(base, destination):
    # Public release files only; never use GitHub or production credentials.
    if not base.startswith('https://github.com/ahe6/svdex-data/releases/download/'):
        raise ValueError('Expected the public SVDEX data release URL')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    def download(name):
        with urllib.request.urlopen(base.rstrip('/') + '/' + name, timeout=60) as response:
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError('Download exceeds safety limit')
        return body
    manifest_bytes = download('manifest.json')
    manifest = json.loads(manifest_bytes)
    if manifest.get('schema_version') != VERSION or set(manifest.get('files', {})) != set(TABLES):
        raise ValueError('Invalid remote manifest')
    for spec in [*manifest['files'].values(), manifest['removed']]:
        if not re.fullmatch(r'[a-z]+\.[A-Za-z0-9]+\.(?:jsonl\.gz|json)', spec['name']):
            raise ValueError('Unsafe remote filename')
        (destination / spec['name']).write_bytes(download(spec['name']))
    (destination / 'manifest.json').write_bytes(manifest_bytes)
    read_snapshot(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export')
    export.add_argument('--site', default='https://svdex.com')
    export.add_argument('--output', required=True)
    export.add_argument('--previous')
    download = commands.add_parser('download')
    download.add_argument('--release', default='https://github.com/ahe6/svdex-data/releases/download/latest')
    download.add_argument('--output', required=True)
    load = commands.add_parser('load')
    load.add_argument('snapshot')
    load.add_argument('database')
    verify = commands.add_parser('verify')
    verify.add_argument('snapshot')
    args = parser.parse_args()
    if args.command == 'export':
        started = utcnow()
        client = GuestClient(args.site)
        manifest = write_snapshot(collect(client), args.output, client.base, started, args.previous)
        print(json.dumps({name: spec['rows'] for name, spec in manifest['files'].items()}, sort_keys=True))
    elif args.command == 'download':
        download_snapshot(args.release, args.output)
    elif args.command == 'load':
        load_sqlite(args.snapshot, args.database)
        print(f'Loaded public snapshot into {args.database}')
    else:
        manifest, _ = read_snapshot(args.snapshot)
        print('Verified snapshot ' + manifest['snapshot_id'])


if __name__ == '__main__':
    main()
