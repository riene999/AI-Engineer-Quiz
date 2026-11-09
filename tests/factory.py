"""Deterministic catalog builder for search/export tests.

Rows are written straight to SQLite (no blobs or thumbnails) and indexed
with the real ``ingest.reindex_fts``, so searches see exactly what the app
would. Every name a Catalog hands out carries its random namespace, which
keeps tests that share one database from seeing each other's rows.
"""
from __future__ import annotations

import uuid as uuidlib
from typing import Iterable, Optional

from keepstack import ingest
from keepstack.audit import now_iso
from keepstack.db import transaction


class Catalog:
    def __init__(self) -> None:
        self.ns = uuidlib.uuid4().hex[:10]

    def tag(self, name: str) -> str:
        """A tag name unique to this catalog (as stored: lower-case)."""
        return f"{name}-{self.ns}".lower()

    def collection(self, name: str) -> int:
        with transaction() as conn:
            cur = conn.execute(
                "INSERT INTO collections (uuid, name, kind, created_at) VALUES (?, ?, 'folder', ?)",
                (str(uuidlib.uuid4()), f"{name} {self.ns}", now_iso()))
            return cur.lastrowid

    def asset(
        self,
        title: str,
        *,
        description: Optional[str] = None,
        filename: Optional[str] = None,
        media_type: str = "image",
        tags: Iterable[str] = (),
        collections: Iterable[int] = (),
        created_at: Optional[str] = None,
        status: str = "active",
    ) -> str:
        uid = str(uuidlib.uuid4())
        created = created_at or now_iso()
        with transaction() as conn:
            cur = conn.execute(
                """INSERT INTO assets (uuid, sha256, filename, original_filename, mime_type, media_type,
                                       ext, size, title, description, status, storage_key,
                                       created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'jpg', 1, ?, ?, ?, ?, ?, ?)""",
                (uid, uid.replace("-", ""), filename or f"{uid[:8]}.jpg", filename or f"{uid[:8]}.jpg",
                 "image/jpeg" if media_type == "image" else "application/pdf", media_type,
                 title, description, status, f"test/{uid}", created, created))
            asset_id = cur.lastrowid
            for name in tags:
                conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
                tid = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
                conn.execute("INSERT INTO asset_tags (asset_id, tag_id, source) VALUES (?, ?, 'manual')",
                             (asset_id, tid))
            for cid in collections:
                conn.execute("INSERT INTO collection_assets (collection_id, asset_id, added_at) "
                             "VALUES (?, ?, ?)", (cid, asset_id, created))
        ingest.reindex_fts(asset_id)
        return uid
