# SVDEX public dataset

Download and query the public content of [SVDEX](https://svdex.com) without
crawling every page. The latest snapshot refreshes daily, around **03:17 UTC**
(GitHub schedules can be delayed), and after changes to these tools reach main.

[Download manifest](https://github.com/ahe6/svdex-data/releases/download/latest/manifest.json)
· [Latest release files](https://github.com/ahe6/svdex-data/releases/tag/latest)
· [Application source](https://github.com/ahe6/svdex)

## Download and load

Python **3.10+**, with its standard library, is enough. No dependencies, API keys,
SVDEX account, Docker or production access are needed.

```sh
git clone https://github.com/ahe6/svdex-data.git
cd svdex-data
python3 dataset.py download --output download
python3 dataset.py load download public.sqlite
```

The loader verifies every checksum, row count and topic/post relationship before
atomically replacing the destination SQLite database. Loading each new snapshot
replaces the old tables completely, including records that are no longer public.
Use a database dedicated to this dataset: the destination file is replaced.

Example queries (SQLite 3.38+ includes JSON functions by default):

```sql
SELECT id, json_extract(data, '$.name') AS company,
       json_extract(data, '$.total_funding_usd') AS funding
FROM companies;

SELECT id, title, terms FROM offerings;

SELECT p.id, json_extract(t.data, '$.title') AS topic,
       json_extract(p.data, '$.username') AS author,
       json_extract(p.data, '$.cooked') AS body_html
FROM posts p JOIN topics t
  ON json_extract(p.data, '$.topic_id') = t.id;
```

Python's built-in SQLite module works too:

```python
import sqlite3
with sqlite3.connect('public.sqlite') as db:
    print(db.execute('SELECT id, title FROM offerings').fetchall())
```

## Contents and format

Each gzip-compressed JSON Lines file contains one current record per line:

| File/table | Contents |
| --- | --- |
| categories | Guest-visible categories, parent relationships and descriptions |
| tags | Public tag names, descriptions and publicly exposed tag groups |
| topics | Public topic metadata, category/tag links, offering terms, product/pre-sale details |
| posts | Current public post bodies and replies, authors, timestamps and reply relationships |
| companies | Public curated company profiles and disclosed business facts |
| funds | Public curated fund profiles and disclosed business facts |
| people | Public curated people, background, organizations and sourced estimates when present |
| authors | Public author identities needed to attribute the exported posts |

The manifest records schema version, source, observation start/end times, file
names, SHA-256 digests, byte sizes and row counts. SVDEX native IDs remain stable
within their respective table; an account may appear in both authors and a curated
directory. `topics.tag_ids` joins to `tags.id`; category IDs join to categories;
posts join to topics and authors. Organization associations retain the public
names, roles and links exposed by SVDEX. SQL views `offerings`, `products` and
`replies` make common queries easier. Unknown financial facts remain null/empty;
they are never replaced with invented values or zero.

Posts contain Discourse's rendered `cooked` HTML, not edit histories or internal
raw fields. Treat all content as untrusted when displaying it. Original source
URLs remain attached to records. Images/attachments remain links in the content;
their binary files are not mirrored. Discourse system posts use their native
author IDs, which can be negative. Account IDs link identities, not ownership or
verification. Financial figures and personal estimates are source disclosures,
not independently verified facts.

## Public scope and removals

The exporter has **no login, API-key or cookie support**. It reads guest endpoints
and constructs every output object from an explicit field allowlist. It skips
restricted/unlisted/deleted topics, hidden/deleted posts, staff whispers and
private-message topics. Pagination and full post streams are checked; missing
pages fail the job rather than publish a known partial snapshot.

No account email/IP/password data, private groups/messages/chat, private
offerings, applications, questionnaire answers, moderation records, bids,
individual interest records, interest aggregates or bookmarks are exported.
Public posts may themselves contain contact information their author published.
This is a point-in-time public export, not an anonymized dataset or full backup.
Collection spans multiple API requests; it is not an atomic database transaction.

Only the latest snapshot is intentionally retained in Releases. Payloads are
uploaded under new versioned names; the stable manifest is replaced last. Prior
payloads are removed after independently verifying the new public download.
Git history contains code/documentation, never daily dataset dumps. If publication
fails after switching the manifest, old files may remain until the next successful
cleanup. Previously downloaded copies cannot be recalled.

`removed.<snapshot>.json` lists IDs absent since the **immediately preceding**
snapshot, without republishing their old text. Absence can mean deletion, hiding,
a visibility/classification change, or removal from a public directory; it does
not establish why a record disappeared. Clients that skip a snapshot must replace
their full local copy rather than rely only on that one-step removal list.
If a download overlaps an update and a versioned file is no longer available,
retry from a fresh manifest. Keep downloaded historical copies private and honor
corrections and removal requests. Contact **austin@svdex.com** for a correction or
public-export concern.

## Rights

The **exporter/loader code** is GPL-2.0-or-later, matching SVDEX. The GPL does **not**
license posts, profile material, logos, attachments or other dataset content.
Original rights holders retain their rights; public visibility alone does not
grant unrestricted republication rights. Source URLs and author attribution are
included so researchers can trace records. See SVDEX's
[Terms](https://svdex.com/tos) and [Privacy Policy](https://svdex.com/privacy).

## Contribute and run your own export

```sh
python3 -m unittest discover -s tests -v
python3 dataset.py export --site https://svdex.com --output dist
python3 dataset.py verify dist
python3 dataset.py load dist public.sqlite
```

Output directories must not already exist. Use `--previous path/to/old-snapshot`
to generate a one-step removal list. Local development origins on loopback HTTP
are supported; never publish a local-fixture export as production data.

Tests cover injected private fields, restricted/unlisted/PM topics, whispers,
deleted/hidden replies, full-stream pagination, hidden profiles, broken files,
path traversal, SQLite reloads and removal propagation. Add privacy regression
tests whenever adding a field. Do not add raw API responses or real private data
to fixtures. New fields are opt-in; schema-breaking changes require a version bump.

The publishing workflow runs only on this repository's main branch after tests.
It uses GitHub's temporary token to replace this repository's release assets;
no AWS or SVDEX credentials are needed. Fork pull requests run tests only. Changes
to this repository cannot deploy or modify the SVDEX application. Propose changes
in a pull request; report a suspected private-data leak directly to the contact
above rather than posting the data in a public issue.
