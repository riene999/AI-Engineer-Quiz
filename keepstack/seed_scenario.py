"""Deterministic data for the "harbor campaign" search/export scenario.

Run after (or instead of) the demo seed::

    python -m keepstack seed-scenario

Creates the collection "Harbor Campaign" and 30 images that exercise every
search rule: required tags (approved + campaign), an excluded tag
(internal), the keyword in title / description / tags, identical
created_at values across a page boundary, a deleted asset, an asset in
another collection, and CSV-hostile text (Chinese, commas, quotes,
newlines). With page size 24 the matching set spans two pages.

Expected for: q=harbor, collection=Harbor Campaign, tag=approved,
tag=campaign, exclude_tag=internal  ->  26 matches.
Re-running is a no-op once the collection exists.
"""
from __future__ import annotations

import io
import uuid as uuidlib

from PIL import Image, ImageDraw

from . import ingest
from .audit import now_iso
from .db import get_conn, init_db, transaction

COLLECTION = "Harbor Campaign"
OTHER_COLLECTION = "Harbor Archive"
TIED_AT = "2026-04-01T09:00:00+00:00"


def _image(i: int, label: str) -> bytes:
    # Distinct colour per index keeps every blob unique (ingest dedups by sha256).
    img = Image.new("RGB", (480, 320), (40 + (i * 37) % 200, 60 + (i * 53) % 180, 90 + (i * 71) % 160))
    ImageDraw.Draw(img).text((20, 280), label[:40], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _rows() -> list[dict]:
    rows = []
    # 24 straightforward matches; 12 of them share TIED_AT so the tie straddles page 1/2.
    for i in range(24):
        rows.append({
            "title": f"Harbor campaign shot {i + 1:02d}",
            "tags": ["approved", "campaign", "harbor"],
            "created_at": TIED_AT if 6 <= i < 18 else f"2026-03-{i + 1:02d}T08:00:00+00:00",
        })
    rows += [
        # Matches through the description only; CSV-hostile text.
        {"title": "港口晨光, \"Pier 7\"", "description": "Taken from the harbor wall,\nsecond line, 中文说明",
         "tags": ["approved", "campaign"], "created_at": "2026-04-02T10:00:00+00:00"},
        # Matches through a tag only.
        {"title": "Lighthouse at dusk", "tags": ["approved", "campaign", "harbor"],
         "created_at": "2026-04-03T10:00:00+00:00"},
        # Excluded: internal.
        {"title": "Harbor internal draft", "tags": ["approved", "campaign", "internal"],
         "created_at": "2026-04-04T10:00:00+00:00"},
        # Missing campaign.
        {"title": "Harbor approved only", "tags": ["approved"], "created_at": "2026-04-05T10:00:00+00:00"},
        # Deleted.
        {"title": "Harbor deleted", "tags": ["approved", "campaign"], "status": "deleted",
         "created_at": "2026-04-06T10:00:00+00:00"},
        # Right tags and keyword, other collection.
        {"title": "Harbor in the archive", "tags": ["approved", "campaign"], "collection": OTHER_COLLECTION,
         "created_at": "2026-04-07T10:00:00+00:00"},
    ]
    return rows


def run_seed_scenario() -> None:
    init_db()
    conn = get_conn()
    if conn.execute("SELECT 1 FROM collections WHERE name = ?", (COLLECTION,)).fetchone():
        print(f"'{COLLECTION}' already exists; nothing to do.")
        return
    admin = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()

    cols = {}
    with transaction() as c:
        for name in (COLLECTION, OTHER_COLLECTION):
            cols[name] = c.execute(
                "INSERT INTO collections (uuid, name, description, kind, created_by, created_at) "
                "VALUES (?, ?, ?, 'folder', ?, ?)",
                (str(uuidlib.uuid4()), name, "Search/export scenario data",
                 admin["id"] if admin else None, now_iso())).lastrowid

    for i, row in enumerate(_rows()):
        asset = ingest.ingest_stream(io.BytesIO(_image(i, row["title"])), f"harbor_scenario_{i + 1:02d}.jpg",
                                     user=dict(admin) if admin else None, run_ai=False, title=row["title"])
        with transaction() as c:
            c.execute("UPDATE assets SET description = ?, status = ?, created_at = ?, updated_at = ? WHERE id = ?",
                      (row.get("description"), row.get("status", "active"), row["created_at"],
                       row["created_at"], asset["id"]))
            for name in row["tags"]:
                c.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
                tid = c.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
                c.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?, ?, 'manual')",
                          (asset["id"], tid))
            c.execute("INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) VALUES (?, ?, ?)",
                      (cols[row.get("collection", COLLECTION)], asset["id"], now_iso()))
        ingest.reindex_fts(asset["id"])
        ingest.reindex_embedding(asset["id"])

    print(f"Seeded {len(_rows())} scenario assets into '{COLLECTION}' / '{OTHER_COLLECTION}'.")
    print("Try: keyword 'harbor', collection 'Harbor Campaign', must have approved + campaign, "
          "exclude internal -> 26 matches.")


if __name__ == "__main__":
    run_seed_scenario()
