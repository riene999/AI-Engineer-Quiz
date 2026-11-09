"""Search and discovery.

Three complementary modes over one catalog:

  * keyword  - FTS5 full-text across title, description, tags, metadata
  * filter   - faceted structured filters (media type, tag, status, dates)
  * semantic - embedding cosine ranking ("natural language" / more-like-this)

Facet counts are returned alongside results so the UI can render a live,
drill-down sidebar.
"""
from __future__ import annotations

import csv
import io
from typing import Iterable, Optional

from . import ai
from .db import get_conn

# Every sort ends with a unique key (a.id) so the order is total: rows that
# share a created_at (or title/size) still land on exactly one page.
SORTS = {
    "newest": "a.created_at DESC, a.id DESC",
    "oldest": "a.created_at ASC, a.id ASC",
    "name": "a.title ASC, a.id ASC",
    "largest": "a.size DESC, a.id DESC",
}

EXPORT_FIELDS = ["uuid", "title", "description", "filename", "media_type", "tags", "created_at"]
EXPORT_TAG_SEPARATOR = ";"


def _fts_query(q: str) -> str:
    """Turn user text into a safe FTS5 prefix query."""
    terms = [t for t in "".join(c if c.isalnum() or c.isspace() else " " for c in q).split() if t]
    return " ".join(f'"{t}"*' for t in terms)


def normalize_tags(tags: Optional[Iterable[str]]) -> list[str]:
    """Match how tags are stored (stripped, lower-cased); drop blanks and repeats."""
    out: list[str] = []
    for t in tags or []:
        name = (t or "").strip().lower()
        if name and name not in out:
            out.append(name)
    return out


def _filter_sql(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    collection_id: Optional[int] = None,
    tags: Optional[Iterable[str]] = None,
    exclude_tags: Optional[Iterable[str]] = None,
    status: str = "active",
) -> tuple[str, list]:
    """WHERE clause shared by the list and the export so they always agree.

    Every condition is a predicate on ``a`` (no JOINs), so parameters are
    bound strictly in the order the clauses are appended and no row can be
    duplicated by a one-to-many join.
    """
    where = ["a.status = ?"]
    params: list = [status]

    fts = _fts_query(q)
    if fts:
        where.append("a.id IN (SELECT rowid FROM asset_fts WHERE asset_fts MATCH ?)")
        params.append(fts)
    if media_type:
        where.append("a.media_type = ?")
        params.append(media_type)
    if collection_id is not None:
        where.append("EXISTS (SELECT 1 FROM collection_assets ca"
                     " WHERE ca.asset_id = a.id AND ca.collection_id = ?)")
        params.append(collection_id)

    include = normalize_tags(tags)
    if include:
        # (asset_id, tag_id) is the primary key and tag names are unique, so
        # counting matches equals counting distinct required tags present.
        marks = ",".join("?" * len(include))
        where.append("(SELECT COUNT(*) FROM asset_tags at JOIN tags t ON t.id = at.tag_id"
                     f" WHERE at.asset_id = a.id AND t.name IN ({marks})) = ?")
        params.extend([*include, len(include)])
    exclude = normalize_tags(exclude_tags)
    if exclude:
        marks = ",".join("?" * len(exclude))
        where.append("NOT EXISTS (SELECT 1 FROM asset_tags at JOIN tags t ON t.id = at.tag_id"
                     f" WHERE at.asset_id = a.id AND t.name IN ({marks}))")
        params.extend(exclude)

    return " AND ".join(where), params


def search(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
    exclude_tags: Optional[Iterable[str]] = None,
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
    mode: str = "keyword",
    limit: int = 60,
    offset: int = 0,
) -> dict:
    conn = get_conn()

    # Semantic mode ranks the candidate pool by embedding similarity.
    if mode == "semantic" and q.strip():
        return _semantic_search(q, media_type, status, limit, offset)

    include = [*(tags or []), *([tag] if tag else [])]
    where_sql, params = _filter_sql(q=q, media_type=media_type, collection_id=collection_id,
                                    tags=include, exclude_tags=exclude_tags, status=status)
    # The chosen sort applies with or without a keyword (no implicit bm25).
    order = SORTS.get(sort, SORTS["newest"])

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM assets a WHERE {where_sql}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"""SELECT a.* FROM assets a WHERE {where_sql}
            ORDER BY {order} LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    ).fetchall()

    return {
        "total": total,
        "items": [_card(dict(r)) for r in rows],
        "facets": _facets(status),
        "mode": mode,
        "sort": sort if sort in SORTS else "newest",
        "limit": limit,
        "offset": offset,
    }


def export_rows(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
    exclude_tags: Optional[Iterable[str]] = None,
    collection_id: Optional[int] = None,
    sort: str = "newest",
) -> list[dict]:
    """Every matching active asset, in list order, with its tag names."""
    conn = get_conn()
    where_sql, params = _filter_sql(q=q, media_type=media_type, collection_id=collection_id,
                                    tags=tags, exclude_tags=exclude_tags)
    order = SORTS.get(sort, SORTS["newest"])
    rows = [dict(r) for r in conn.execute(
        f"SELECT a.* FROM assets a WHERE {where_sql} ORDER BY {order}", params)]
    tag_map: dict[int, list[str]] = {}
    for r in conn.execute(
        f"""SELECT at.asset_id, t.name FROM asset_tags at JOIN tags t ON t.id = at.tag_id
            WHERE at.asset_id IN (SELECT a.id FROM assets a WHERE {where_sql})
            ORDER BY t.name""", params):
        tag_map.setdefault(r["asset_id"], []).append(r["name"])
    for r in rows:
        r["tags"] = tag_map.get(r["id"], [])
    return rows


def export_csv(**filters) -> str:
    """CSV text (RFC 4180 quoting, CRLF rows); header is written even for zero rows."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_FIELDS)
    for r in export_rows(**filters):
        w.writerow([
            r["uuid"], r["title"] or "", r["description"] or "", r["filename"] or "",
            r["media_type"] or "", EXPORT_TAG_SEPARATOR.join(r["tags"]), r["created_at"],
        ])
    return buf.getvalue()


def _semantic_search(q, media_type, status, limit, offset) -> dict:
    conn = get_conn()
    qvec = ai.embed(q)
    where = ["status = ?", "embedding IS NOT NULL"]
    params: list = [status]
    if media_type:
        where.append("media_type = ?")
        params.append(media_type)
    rows = conn.execute(
        f"SELECT * FROM assets WHERE {' AND '.join(where)}", params
    ).fetchall()
    scored = []
    for r in rows:
        score = ai.cosine(qvec, ai.unpack_embedding(r["embedding"]))
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    page = scored[offset:offset + limit]
    items = []
    for score, r in page:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        items.append(card)
    return {"total": len(scored), "items": items, "facets": _facets(status), "mode": "semantic"}


def more_like(asset_id: int, limit: int = 12) -> list[dict]:
    conn = get_conn()
    base = conn.execute("SELECT embedding FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not base or not base["embedding"]:
        return []
    bvec = ai.unpack_embedding(base["embedding"])
    rows = conn.execute(
        "SELECT * FROM assets WHERE id != ? AND status = 'active' AND embedding IS NOT NULL",
        (asset_id,),
    ).fetchall()
    scored = [(ai.cosine(bvec, ai.unpack_embedding(r["embedding"])), r) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in scored[:limit]:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        out.append(card)
    return out


def _facets(status: str) -> dict:
    conn = get_conn()
    media = conn.execute(
        "SELECT media_type, COUNT(*) c FROM assets WHERE status = ? GROUP BY media_type ORDER BY c DESC",
        (status,),
    ).fetchall()
    tags = conn.execute(
        """SELECT t.name, COUNT(*) c FROM tags t
           JOIN asset_tags at ON at.tag_id = t.id
           JOIN assets a ON a.id = at.asset_id AND a.status = ?
           GROUP BY t.name ORDER BY c DESC LIMIT 25""",
        (status,),
    ).fetchall()
    return {
        "media_types": [{"value": r["media_type"], "count": r["c"]} for r in media],
        "tags": [{"value": r["name"], "count": r["c"]} for r in tags],
    }


def _card(a: dict) -> dict:
    """Compact asset representation for grid/list views."""
    return {
        "id": a["id"],
        "uuid": a["uuid"],
        "title": a["title"],
        "filename": a["filename"],
        "media_type": a["media_type"],
        "mime_type": a["mime_type"],
        "ext": a["ext"],
        "size": a["size"],
        "width": a["width"],
        "height": a["height"],
        "alt_text": a["alt_text"],
        "status": a["status"],
        "created_at": a["created_at"],
    }
