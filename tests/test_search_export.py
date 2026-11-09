"""Combined filters, stable paging, CSV export and edit-then-search.

Each test builds its own small catalog under a random namespace (its own
collection / tag names) through ``tests.factory``, so it is independent of
other tests sharing the throwaway database. Run with:  python -m pytest -q
"""
import csv
import io
import os
import tempfile

os.environ.setdefault("KEEPSTACK_DATA_DIR", tempfile.mkdtemp(prefix="keepstack-test-"))
os.environ["KEEPSTACK_AI_ENABLED"] = "false"
os.environ["KEEPSTACK_OCR_ENABLED"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from keepstack import search  # noqa: E402
from keepstack.app import app  # noqa: E402
from keepstack.auth import make_token  # noqa: E402
from keepstack.db import get_conn, init_db  # noqa: E402

from .factory import Catalog  # noqa: E402


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:  # runs the startup hook, which creates admin
        admin = get_conn().execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
        c.headers["Authorization"] = "Bearer " + make_token(admin["id"], "admin")
        yield c


@pytest.fixture
def cat():
    init_db()
    return Catalog()


def _uuids(res):
    return [a["uuid"] for a in res["items"]]


def _all_pages(client, params, page_size):
    """Walk the list API page by page; return (uuids in order, reported totals)."""
    seen, totals, offset = [], set(), 0
    while True:
        r = client.get("/api/assets", params={**params, "limit": page_size, "offset": offset})
        assert r.status_code == 200, r.text
        body = r.json()
        totals.add(body["total"])
        seen.extend(_uuids(body))
        if len(body["items"]) < page_size:
            return seen, totals
        offset += page_size


def _csv(resp):
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.content.startswith(b"\xef\xbb\xbf")  # BOM for Excel
    return list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"), newline="")))


# ---------------------------------------------------------------------------
# Combined filters
# ---------------------------------------------------------------------------

def test_business_scenario_combined_filters(cat):
    c, other = cat.collection("campaign"), cat.collection("elsewhere")
    approved, campaign, internal = cat.tag("approved"), cat.tag("campaign"), cat.tag("internal")

    hit_title = cat.asset("Harbor at dawn", tags=[approved, campaign], collections=[c])
    hit_desc = cat.asset("Pier lights", description="taken from the harbor wall",
                         tags=[approved, campaign], collections=[c], media_type="document")
    cat.asset("Harbor internal", tags=[approved, campaign, internal], collections=[c])
    cat.asset("Harbor only approved", tags=[approved], collections=[c])
    cat.asset("Harbor other collection", tags=[approved, campaign], collections=[other])
    cat.asset("Mountain no keyword", tags=[approved, campaign], collections=[c])
    cat.asset("Harbor deleted", tags=[approved, campaign], collections=[c], status="deleted")
    cat.asset("Harbor near-miss tag", tags=[approved + "-old", campaign], collections=[c])

    base = dict(q="harbor", collection_id=c, tags=[approved, campaign], exclude_tags=[internal])
    assert set(_uuids(search.search(**base))) == {hit_title, hit_desc}
    assert search.search(**base)["total"] == 2

    # Tag input is normalised like stored tags; repeats change nothing.
    messy = dict(base, tags=[f"  {approved.upper()} ", campaign, campaign],
                 exclude_tags=[internal, internal.upper()])
    assert set(_uuids(search.search(**messy))) == {hit_title, hit_desc}

    # Media type narrows further.
    assert _uuids(search.search(**base, media_type="image")) == [hit_title]

    # Same tag both included and excluded -> nothing.
    clash = search.search(**dict(base, exclude_tags=[internal, campaign]))
    assert clash["total"] == 0 and clash["items"] == []


def test_blank_keyword_and_each_condition_alone(cat):
    c = cat.collection("solo")
    a, b = cat.tag("a"), cat.tag("b")
    x = cat.asset("first", tags=[a], collections=[c])
    y = cat.asset("second", tags=[a, b], collections=[c])
    z = cat.asset("third", tags=[b], collections=[c])

    assert set(_uuids(search.search(q="   ", collection_id=c))) == {x, y, z}
    assert set(_uuids(search.search(collection_id=c, tags=[a]))) == {x, y}
    assert set(_uuids(search.search(collection_id=c, tags=[a, b]))) == {y}
    assert set(_uuids(search.search(collection_id=c, exclude_tags=[a]))) == {z}
    assert set(_uuids(search.search(collection_id=c, exclude_tags=[a, b]))) == set()
    # A tag filter alone (no collection) works too: these tags are unique to this test.
    assert set(_uuids(search.search(tags=[b]))) == {y, z}


def test_special_characters_in_keyword_do_not_error(client, cat):
    c = cat.collection("chars")
    hit = cat.asset("Harbor 码头 \"quoted\"", collections=[c])
    for q in ['"harbor"', "harbor*", "harbor OR", "NEAR(harbor", "码头", "'; DROP TABLE assets; --", "!!!"]:
        r = client.get("/api/assets", params={"q": q, "collection_id": c})
        assert r.status_code == 200, (q, r.text)
    assert _uuids(client.get("/api/assets", params={"q": '"harbor"', "collection_id": c}).json()) == [hit]
    # Input with no searchable term is treated like a blank keyword.
    assert client.get("/api/assets", params={"q": "!!!", "collection_id": c}).json()["total"] == 1


# ---------------------------------------------------------------------------
# Sorting and paging
# ---------------------------------------------------------------------------

def test_pages_cover_every_row_once_with_identical_created_at(client, cat):
    c = cat.collection("paging")
    stamps = ["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", "2026-01-03T00:00:00+00:00"]
    # 23 rows over 3 timestamps: lots of ties, and a page size that does not divide evenly.
    made = [cat.asset(f"harbor shot {i}", collections=[c], created_at=stamps[i % 3]) for i in range(23)]

    for sort in ("newest", "oldest"):
        full = search.search(collection_id=c, sort=sort, limit=200)
        assert full["total"] == 23
        for size in (1, 5, 7, 23):
            pages, totals = _all_pages(client, {"collection_id": c, "sort": sort}, size)
            assert totals == {23}
            assert pages == _uuids(full)          # same order, no gaps, no repeats
            assert len(set(pages)) == 23 == len(made)

    newest = _uuids(search.search(collection_id=c, sort="newest", limit=200))
    oldest = _uuids(search.search(collection_id=c, sort="oldest", limit=200))
    assert newest == oldest[::-1]
    # Created_at actually drives the order.
    created = {u: s for u, s in zip(made, (stamps[i % 3] for i in range(23)))}
    assert [created[u] for u in newest] == sorted(created.values(), reverse=True)


def test_keyword_results_follow_selected_sort(cat):
    c = cat.collection("kwsort")
    # The oldest row is the most relevant one; bm25 order would put it first for "newest".
    old = cat.asset("harbor harbor harbor harbor", collections=[c], created_at="2025-01-01T00:00:00+00:00")
    mid = cat.asset("harbor and other words here", collections=[c], created_at="2025-06-01T00:00:00+00:00")
    new = cat.asset("a long title that mentions harbor once", collections=[c],
                    created_at="2026-01-01T00:00:00+00:00")
    assert _uuids(search.search(q="harbor", collection_id=c, sort="newest")) == [new, mid, old]
    assert _uuids(search.search(q="harbor", collection_id=c, sort="oldest")) == [old, mid, new]


def test_paging_past_the_end_and_invalid_params(client, cat):
    c = cat.collection("bounds")
    cat.asset("only one", collections=[c])
    r = client.get("/api/assets", params={"collection_id": c, "offset": 50, "limit": 10})
    assert r.status_code == 200 and r.json()["total"] == 1 and r.json()["items"] == []
    for bad in ({"limit": 0}, {"limit": 201}, {"limit": "x"}, {"offset": -1},
                {"sort": "random"}, {"collection_id": 0}, {"collection_id": "abc"}):
        assert client.get("/api/assets", params=bad).status_code == 422, bad
        assert client.get("/api/assets/export.csv", params=bad).status_code in (200, 422)
    assert client.get("/api/assets/export.csv", params={"sort": "random"}).status_code == 422


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def test_export_matches_list_across_pages(client, cat):
    c = cat.collection("export")
    keep, drop = cat.tag("keep"), cat.tag("drop")
    stamp = "2026-03-03T03:03:03+00:00"
    for i in range(13):
        tags = [keep] + ([drop] if i % 4 == 0 else [])
        cat.asset(f"harbor export {i}", collections=[c], tags=tags, created_at=stamp if i % 2 else None)

    params = {"q": "harbor", "collection_id": c, "tag": keep, "exclude_tag": drop}
    for sort in ("newest", "oldest"):
        listed, totals = _all_pages(client, {**params, "sort": sort}, 4)
        rows = _csv(client.get("/api/assets/export.csv", params={**params, "sort": sort}))
        assert [r["uuid"] for r in rows] == listed
        assert totals == {len(rows)} and len(rows) == 9


def test_export_fields_escaping_and_tags(client, cat):
    c = cat.collection("escape")
    t1, t2 = cat.tag("zeta"), cat.tag("alpha")
    title = '港口, "晨光"'
    desc = 'line one\nline two, with "quotes"\r\nand 中文'
    u = cat.asset(title, description=desc, filename="港口,1.jpg", tags=[t1, t2], collections=[c],
                  created_at="2026-05-05T05:05:05+00:00")

    resp = client.get("/api/assets/export.csv", params={"collection_id": c})
    assert "attachment" in resp.headers["content-disposition"]
    rows = _csv(resp)
    assert list(rows[0].keys()) == search.EXPORT_FIELDS
    assert rows == [{
        "uuid": u, "title": title, "description": desc, "filename": "港口,1.jpg",
        "media_type": "image", "tags": ";".join(sorted([t1, t2])),
        "created_at": "2026-05-05T05:05:05+00:00",
    }]


def test_export_empty_result_has_header_only(client, cat):
    c = cat.collection("empty")
    resp = client.get("/api/assets/export.csv", params={"collection_id": c})
    assert resp.content.decode("utf-8-sig").strip() == ",".join(search.EXPORT_FIELDS)


def test_export_and_list_require_login(client):
    anon = TestClient(app)
    assert anon.get("/api/assets/export.csv").status_code == 401
    assert anon.get("/api/assets").status_code == 401
    bad = {"Authorization": "Bearer nonsense"}
    assert anon.get("/api/assets/export.csv", headers=bad).status_code == 401


# ---------------------------------------------------------------------------
# Edits are visible to the next query and export
# ---------------------------------------------------------------------------

def test_edits_through_api_show_up_in_search_and_export(client, cat):
    c = cat.collection("edits")
    approved = cat.tag("approved")
    u = cat.asset("plain title", collections=[c])
    word = "lighthouse" + cat.ns  # a keyword nothing else in the db contains

    def listed(**p):
        return _uuids(client.get("/api/assets", params={"collection_id": c, **p}).json())

    def exported(**p):
        return [r["uuid"] for r in _csv(client.get("/api/assets/export.csv",
                                                    params={"collection_id": c, **p}))]

    assert listed(q=word) == [] and listed(tag=approved) == []

    assert client.patch(f"/api/assets/{u}", json={"title": f"New {word} title"}).status_code == 200
    assert listed(q=word) == exported(q=word) == [u]
    assert _csv(client.get("/api/assets/export.csv", params={"collection_id": c}))[0]["title"] \
        == f"New {word} title"

    assert client.patch(f"/api/assets/{u}", json={"title": "back to plain",
                                                  "description": f"now in {word} description"}
                        ).status_code == 200
    assert listed(q=word) == [u]
    assert listed(q="plain") == [u]

    assert client.post(f"/api/assets/{u}/tags", json={"name": approved.upper()}).status_code == 200
    assert listed(tag=approved) == exported(tag=approved) == [u]
    assert listed(exclude_tag=approved) == []
    assert listed(q=approved) == [u]  # tags are part of the FTS document

    assert client.delete(f"/api/assets/{u}/tags/{approved}").status_code == 200
    assert listed(tag=approved) == exported(tag=approved) == []
    assert listed(exclude_tag=approved) == [u]

    # Deleting (status != active) removes it from both.
    assert client.delete(f"/api/assets/{u}").status_code == 200
    assert listed() == exported() == []


# ---------------------------------------------------------------------------
# Reproducible scenario data (python -m keepstack seed-scenario)
# ---------------------------------------------------------------------------

def test_seed_scenario_matches_documented_counts(client):
    from keepstack import seed_scenario

    seed_scenario.run_seed_scenario()
    seed_scenario.run_seed_scenario()  # idempotent
    cid = get_conn().execute("SELECT id FROM collections WHERE name = ?",
                             (seed_scenario.COLLECTION,)).fetchone()["id"]
    params = {"q": "harbor", "collection_id": cid, "tag": ["approved", "campaign"],
              "exclude_tag": "internal", "sort": "oldest"}
    listed, totals = _all_pages(client, params, 24)
    assert totals == {26} and len(listed) == len(set(listed)) == 26
    rows = _csv(client.get("/api/assets/export.csv", params=params))
    assert [r["uuid"] for r in rows] == listed
    assert any("\n" in r["description"] and "中文" in r["description"] for r in rows)
