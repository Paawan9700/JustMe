"""
Unit tests for favourite voices: the matching math (shared/voice_match.py),
the pure parts of backend/app/services/favorite_service.py, and route-level
checks for /api/favorites and the favourite hooks on the job routes.

Hermetic like test_auth.py: dummy env vars before import; Mongo and R2 are
never touched (service calls are swapped for in-memory fakes). Run with:

    ./venv/bin/python backend/tests/test_favorites.py
"""

import asyncio
import os
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))      # for `shared`
sys.path.insert(0, str(BACKEND))   # for `app`

for var in (
    "MONGO_URL", "DB_NAME", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME", "R2_ENDPOINT_URL",
):
    os.environ.setdefault(var, "test")

from app.core.config import settings  # noqa: E402
from app.services import auth, favorite_service as fs  # noqa: E402
from shared import voice_match as vm  # noqa: E402

SECRET = "unit-test-secret-" + "x" * 32
NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)
MODEL, RECIPE = "pyannote/wespeaker-voxceleb-resnet34-LM", 1


def unit(*xs):
    return vm.normalize(list(xs))


# Voices in 3-D: MANGLA and NEAR share 0.8 cosine (a "likely" match), OTHER
# is orthogonal to MANGLA.
MANGLA = unit(1, 0, 0)
NEAR = unit(0.8, 0.6, 0)
OTHER = unit(0, 1, 0)
FAR = unit(0, 0, 1)


def vprint(vec, speech=120.0, model=MODEL, recipe=RECIPE):
    return {"centroid": vm.pack(vec), "speech_sec": speech, "model": model,
            "recipe_version": recipe}


def fav(fid, *vecs, source_job="old-job", label="SPEAKER_01", name=None, model=MODEL):
    samples = [{"centroid": vm.pack(v), "model": model, "recipe_version": RECIPE,
                "job_id": source_job, "label": label} for v in vecs]
    return {"favorite_id": fid, "name": name, "source_job_id": source_job,
            "source_speaker_label": label, "original_sample": samples[0],
            "learned_samples": samples[1:]}


# ---- matching math ------------------------------------------------------------

def test_best_favourite_wins_and_threshold_applies():
    speakers = {"A": MANGLA, "B": OTHER, "C": FAR}
    favourites = [("f-mangla", [MANGLA]), ("f-other", [unit(0.1, 1, 0)])]
    m = vm.match_speakers(speakers, favourites)
    assert m["A"]["favorite_id"] == "f-mangla" and m["A"]["strength"] == "strong"
    assert m["B"]["favorite_id"] == "f-other"
    assert "C" not in m                            # nobody sounds like FAR


def test_likely_vs_strong_and_below_show():
    m = vm.match_speakers({"A": NEAR}, [("f", [MANGLA])])
    assert m["A"]["strength"] == "strong"          # 0.80 is the strong edge
    m = vm.match_speakers({"A": unit(0.75, 0.66, 0)}, [("f", [MANGLA])])
    assert m["A"]["strength"] == "likely", m
    assert vm.match_speakers({"A": unit(0.6, 0.8, 0)}, [("f", [MANGLA])]) == {}


def test_one_person_split_into_two_labels_fills_both_cards():
    m = vm.match_speakers({"S1": MANGLA, "S2": NEAR}, [("f", [MANGLA])])
    assert set(m) == {"S1", "S2"} and {x["favorite_id"] for x in m.values()} == {"f"}


def test_best_sample_counts():
    # The favourite learned a second sample that matches today's voice.
    m = vm.match_speakers({"A": OTHER}, [("f", [MANGLA, OTHER])])
    assert m["A"]["score"] > 0.99


def test_empty_inputs():
    assert vm.match_speakers({}, [("f", [MANGLA])]) == {}
    assert vm.match_speakers({"A": MANGLA}, []) == {}


def test_similarity_matrix_numpy_matches_pure_python():
    rows, cols = [MANGLA, NEAR], [OTHER, FAR, NEAR]
    pure = [[vm.dot(r, c) for c in cols] for r in rows]
    got = vm.similarity_matrix(rows, cols)
    for a, b in zip(pure, got):
        for x, y in zip(a, b):
            assert abs(x - y) < 1e-9


# ---- favourite_service pure parts -----------------------------------------------

def test_box_matches_skips_favourites_saved_from_the_same_job():
    prints = {"S3": vprint(MANGLA)}
    favorites = [fav("f-here", MANGLA, source_job="job-1", label="S3")]
    assert fs.box_matches("job-1", prints, favorites) == {}
    assert "S3" in fs.box_matches("job-2", prints, favorites)


def test_box_matches_ignores_other_models():
    prints = {"S3": vprint(MANGLA)}
    favorites = [fav("f", MANGLA, model="some-other-model")]
    assert fs.box_matches("job-2", prints, favorites) == {}


def test_annotations():
    job = {"job_id": "job-1", "speakers": [
        {"label": "S1", "snippet_key": "k1"},   # matched -> box
        {"label": "S2", "snippet_key": "k2"},   # hearted on this job -> filled, in place
        {"label": "S3", "snippet_key": "k3"},   # plain
        {"label": "S4", "snippet_key": None},   # no snippet -> can't favourite
        {"label": "S5", "snippet_key": "k5"},   # no print -> can't favourite
    ]}
    prints = {lb: vprint(FAR) for lb in ("S1", "S2", "S3", "S4")}
    favorites = [
        fav("f-box", MANGLA, name="Mangla ji"),
        fav("f-here", OTHER, source_job="job-1", label="S2"),
    ]
    matches = {"S1": {"favorite_id": "f-box", "score": 0.93, "strength": "strong"}}
    ann = fs.build_annotations(job, prints, favorites, matches)
    assert ann["S1"]["favorite"] == {"favorite_id": "f-box", "name": "Mangla ji",
                                     "in_box": True, "strength": "strong"}
    assert ann["S2"]["favorite"]["favorite_id"] == "f-here"
    assert ann["S2"]["favorite"]["in_box"] is False
    assert ann["S3"]["favorite"] is None and ann["S3"]["can_favorite"] is True
    assert ann["S4"]["can_favorite"] is False
    assert ann["S5"]["can_favorite"] is False


def test_annotate_only_for_the_owner_while_awaiting_selection():
    owner = {"user_id": "u1", "role": "user"}
    admin = {"user_id": "u9", "role": "admin"}
    job = {"job_id": "j", "user_id": "u1", "status": "AWAITING_SELECTION", "speakers": []}
    # No DB is reachable here, so returning early is the only way these pass.
    assert asyncio.run(fs.annotate(job, admin)) == ({}, None)
    assert asyncio.run(fs.annotate({**job, "status": "DONE"}, owner)) == ({}, None)
    assert asyncio.run(fs.annotate({**job, "user_id": None}, owner)) == ({}, None)


def test_should_learn_needs_confidence_and_enough_speech():
    strong = {"favorite_id": "f", "score": 0.85}
    assert fs.should_learn(strong, {"speech_sec": 90})
    assert not fs.should_learn(strong, {"speech_sec": 30})
    assert not fs.should_learn({"favorite_id": "f", "score": 0.75}, {"speech_sec": 90})
    assert not fs.should_learn(None, {"speech_sec": 90})


def test_clean_name():
    assert fs.clean_name("  Mangla ji ") == "Mangla ji"
    assert fs.clean_name("   ") is None
    assert fs.clean_name(None) is None


# ---- routes ---------------------------------------------------------------------

def _configure():
    settings.allowed_emails = "friend@gmail.com"
    settings.admin_emails = "owner@gmail.com"
    settings.auth_jwt_secret = SECRET
    settings.auth_token_ttl_days = 30
    settings.open_signup = False
    settings.blocked_emails = ""


class _FakeStorage:
    def get_presigned_url(self, key, **_):
        return f"https://r2.test/{key}"


def _client():
    from fastapi.testclient import TestClient

    from app.api import favorites as favorites_api
    from app.api import jobs as jobs_api
    from app.main import app
    from app.services import job_service, user_service

    users = {
        "u-owner": {"user_id": "u-owner", "email": "owner@gmail.com"},
        "u-friend": {"user_id": "u-friend", "email": "friend@gmail.com"},
    }
    store = {"f-1": {"favorite_id": "f-1", "user_id": "u-friend", "name": None,
                     "created_at": NOW, "source_job_id": "job-1",
                     "source_video_title": "Final Trade", "sample_key": "favorites/u-friend/f-1.mp3",
                     "learned_samples": [{}, {}]}}
    calls = {}

    async def get_user(user_id):
        return dict(users[user_id]) if user_id in users else None

    async def list_favorites(user_id):
        return [d for d in store.values() if d["user_id"] == user_id]

    async def get_favorite(user_id, fid):
        d = store.get(fid)
        return d if d and d["user_id"] == user_id else None

    async def create_favorite(user, job_id, label):
        if job_id == "full":
            raise fs.FavoriteError(409, "You have 20 favourite voices")
        if job_id == "someone-elses":
            raise fs.FavoriteError(404, "Job not found")
        return {**store["f-1"], "user_id": user["user_id"], "source_job_id": job_id,
                "learned_samples": []}

    async def rename_favorite(user_id, fid, name):
        d = await get_favorite(user_id, fid)
        return {**d, "name": fs.clean_name(name)} if d else None

    async def delete_favorite(user_id, fid):
        return (await get_favorite(user_id, fid)) is not None

    async def get_job_owner(job_id):
        return {"user_id": "u-friend"} if job_id == "job-1" else None

    async def get_job_hydrated(job_id, viewer=None):
        calls["hydrate_viewer"] = viewer
        return {"job_id": job_id, "status": "AWAITING_SELECTION", "progress": {},
                "created_at": NOW, "updated_at": NOW, "favorites_total": 1,
                "speakers": [{"label": "S1", "total_speaking_sec": 90.0, "segment_count": 3,
                              "can_favorite": True,
                              "favorite": {"favorite_id": "f-1", "name": None,
                                           "in_box": True, "strength": "strong"}}]}

    async def select_speaker(job_id, label):
        return {"ok": True, "job": {"job_id": job_id, "status": "RENDERING"}}

    async def set_task_id(job_id, task_id):
        return None

    async def record_selection_safe(job_id, label, user):
        calls["recorded"] = (job_id, label, user["user_id"])

    user_service.get_user = get_user
    for name, fn in (("list_favorites", list_favorites), ("get_favorite", get_favorite),
                     ("create_favorite", create_favorite), ("rename_favorite", rename_favorite),
                     ("delete_favorite", delete_favorite),
                     ("record_selection_safe", record_selection_safe)):
        setattr(fs, name, fn)
    job_service.get_job_owner = get_job_owner
    job_service.get_job_hydrated = get_job_hydrated
    job_service.select_speaker = select_speaker
    job_service.set_task_id = set_task_id
    jobs_api.enqueue_render_video = lambda job_id: "task-1"
    favorites_api.get_storage = lambda: _FakeStorage()
    return TestClient(app), calls


def _bearer(user_id):
    return {"Authorization": f"Bearer {auth.issue_token(user_id)}"}


def test_favorite_routes_require_a_session():
    _configure()
    client, _ = _client()
    for method, path in (("get", "/api/favorites"), ("get", "/api/favorites/f-1"),
                         ("post", "/api/favorites"), ("patch", "/api/favorites/f-1"),
                         ("delete", "/api/favorites/f-1")):
        r = getattr(client, method)(path)
        assert r.status_code == 401, f"{method.upper()} {path} -> {r.status_code}"


def test_list_get_and_other_users_favourite_is_404():
    _configure()
    client, _ = _client()
    r = client.get("/api/favorites", headers=_bearer("u-friend"))
    assert r.status_code == 200
    [item] = r.json()
    assert item["sample_url"] == "https://r2.test/favorites/u-friend/f-1.mp3"
    assert item["sample_count"] == 3 and item["name"] is None
    assert client.get("/api/favorites/f-1", headers=_bearer("u-friend")).status_code == 200
    assert client.get("/api/favorites/f-1", headers=_bearer("u-owner")).status_code == 404
    assert client.delete("/api/favorites/f-1", headers=_bearer("u-owner")).status_code == 404
    assert client.delete("/api/favorites/f-1", headers=_bearer("u-friend")).status_code == 204


def test_create_maps_refusals_and_rename_clears():
    _configure()
    client, _ = _client()
    h = _bearer("u-friend")
    r = client.post("/api/favorites", json={"job_id": "job-1", "speaker_label": "S1"}, headers=h)
    assert r.status_code == 201 and r.json()["source_job_id"] == "job-1"
    r = client.post("/api/favorites", json={"job_id": "full", "speaker_label": "S1"}, headers=h)
    assert r.status_code == 409 and "20" in r.json()["detail"]
    r = client.post("/api/favorites", json={"job_id": "someone-elses", "speaker_label": "S1"},
                    headers=h)
    assert r.status_code == 404
    r = client.patch("/api/favorites/f-1", json={"name": "  Mangla ji "}, headers=h)
    assert r.json()["name"] == "Mangla ji"
    r = client.patch("/api/favorites/f-1", json={"name": "   "}, headers=h)
    assert r.json()["name"] is None
    r = client.patch("/api/favorites/f-1", json={"name": "x" * 61}, headers=h)
    assert r.status_code == 422


def test_job_read_passes_the_viewer_and_serves_favourite_fields():
    _configure()
    client, calls = _client()
    r = client.get("/api/jobs/job-1", headers=_bearer("u-friend"))
    assert r.status_code == 200, r.text
    assert calls["hydrate_viewer"]["user_id"] == "u-friend"
    body = r.json()
    assert body["favorites_total"] == 1
    assert body["speakers"][0]["favorite"]["in_box"] is True
    assert body["speakers"][0]["can_favorite"] is True


def test_selecting_a_speaker_records_it_for_learning():
    _configure()
    client, calls = _client()
    r = client.post("/api/jobs/job-1/select-speaker", json={"speaker_label": "S1"},
                    headers=_bearer("u-friend"))
    assert r.status_code == 200, r.text
    assert calls["recorded"] == ("job-1", "S1", "u-friend")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
