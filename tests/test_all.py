import hashlib, hmac, json, os, tempfile
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import Job, MessageLog, Order
from app.main import create_app
from app.messenger import SendResult
from app.worker import claim_job, process_job

SECRET = "sekret"
SEED = {
    "posts": [{"page_id": "P1", "post_id": "P1_POST", "enabled": True, "catalog_id": "c"}],
    "catalog": [
        {"catalog_id": "c", "sku": "YEM", "name": "yếm", "aliases": ["yem"], "unit_price": 689000,
         "colors": {"xanh": ["xanh duong"], "hồng": ["hong"]}, "sizes": ["XS", "S"], "in_stock": True},
        {"catalog_id": "c", "sku": "KEP", "name": "kẹp", "aliases": ["kep"], "unit_price": 165000,
         "colors": {}, "sizes": [], "in_stock": True},
    ],
}


class FakeMessenger:
    def __init__(self, result=None):
        self.result, self.calls = result or SendResult("SENT", message_id="m1"), []
    def fetch_comment(self, cid): return None
    def fetch_post(self, pid): return {'message': 'ban yem', 'permalink_url': 'http://x'}
    def private_reply(self, cid, text):
        self.calls.append((cid, text)); return self.result


@pytest.fixture
def env():
    d = tempfile.mkdtemp()
    seed = os.path.join(d, "seed.json"); json.dump(SEED, open(seed, "w"))
    cfg = Settings(verify_token="vt", app_secret=SECRET, page_id="P1", page_access_token="t",
                   graph_version="v21.0", webhook_path="/webhooks/facebook",
                   database_url=f"sqlite:///{d}/t.db", seed_file=seed, currency="VND",
                   max_attempts=3, backoff_base_seconds=0, poll_interval=0.1, send_timeout=1, dry_run=False,
                   default_catalog_id=os.environ.get('T_DEFAULT_CAT', ''), admin_password='pw')
    app = create_app(cfg)
    return cfg, app, TestClient(app), app.state.Session


def payload(cid="C1", msg="2 yếm xanh XS và 2 kẹp", post="P1_POST", frm="U1"):
    v = {"item": "comment", "verb": "add", "comment_id": cid, "post_id": post, "message": msg}
    if frm: v["from"] = {"id": frm}
    return {"object": "page", "entry": [{"id": "P1", "changes": [{"field": "feed", "value": v}]}]}


def post(client, body):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/facebook", content=raw, headers={"X-Hub-Signature-256": sig})


def run(env, messenger):
    cfg, _, _, S = env
    jid = claim_job(S)
    return process_job(S, cfg, messenger, jid) if jid else None


def test_verify(env):
    c = env[2]
    r = c.get("/webhooks/facebook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "123456"})
    assert r.status_code == 200 and r.text == "123456"
    assert c.get("/webhooks/facebook", params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"}).status_code == 403


def test_signature(env):
    c = env[2]
    raw = json.dumps(payload()).encode()
    assert c.post("/webhooks/facebook", content=raw).status_code == 403
    assert c.post("/webhooks/facebook", content=raw, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 403
    with env[3]() as s:
        assert s.execute(select(Job)).first() is None


def test_happy_path_and_replay(env):
    c, m = env[2], FakeMessenger()
    assert post(c, payload()).status_code == 200
    assert post(c, payload()).status_code == 200  # replay
    assert run(env, m) == "SENT"
    assert run(env, m) is None
    assert len(m.calls) == 1 and "1.708.000đ" in m.calls[0][1]
    with env[3]() as s:
        o = s.execute(select(Order)).scalar_one()
        assert o.total == 1708000 and o.status == "READY"
        assert s.execute(select(MessageLog)).scalar_one().status == "SENT"


def test_ignored_events(env):
    c = env[2]
    p = payload(); p["entry"][0]["changes"][0]["value"]["verb"] = "edited"
    post(c, p)
    post(c, {"object": "page", "entry": [{"id": "P1", "changes": [{"field": "feed", "value": {"item": "status"}}]}]})
    post(c, {"object": "page", "entry": "garbage"})
    with env[3]() as s:
        assert s.execute(select(Job)).first() is None


def test_unconfigured_post_and_self_comment(env):
    c, m = env[2], FakeMessenger()
    post(c, payload("C2", post="OTHER")); assert run(env, m) == "SKIPPED"
    post(c, payload("C3", frm="P1")); assert run(env, m) == "SKIPPED"
    assert not m.calls


@pytest.mark.parametrize("msg", ["chấm", "ib", "2 yếm XS", "2 yếm xanh", "yếm xanh XS", "2 yếm xanh hồng XS", "2 áo"])
def test_ambiguous_needs_review(env, msg):
    m = FakeMessenger()
    post(env[2], payload("C9", msg)); assert run(env, m) == "NEEDS_REVIEW"
    assert not m.calls
    with env[3]() as s:
        assert s.execute(select(Order)).scalar_one().status == "NEEDS_REVIEW"


def test_missing_message_no_fetch(env):
    m = FakeMessenger()
    post(env[2], payload("C8", msg=None, frm=None))
    assert run(env, m) == "NEEDS_REVIEW"


def test_4xx_failed_no_retry(env):
    m = FakeMessenger(SendResult("FAILED", error_code="200", error_message="perm"))
    post(env[2], payload()); assert run(env, m) == "FAILED"
    assert run(env, m) is None
    with env[3]() as s:
        assert s.execute(select(MessageLog)).scalar_one().status == "FAILED"


def test_retry_then_success(env):
    m = FakeMessenger(SendResult("RETRY", error_code="429"))
    post(env[2], payload()); assert run(env, m) == "RETRY"
    m.result = SendResult("SENT", message_id="ok")
    assert run(env, m) == "SENT" and len(m.calls) == 2


def test_retry_exhausted_dead(env):
    m = FakeMessenger(SendResult("RETRY", error_code="500"))
    post(env[2], payload())
    outs = [run(env, m) for _ in range(3)]
    assert outs[-1] == "RETRY" and run(env, m) is None
    with env[3]() as s:
        assert s.execute(select(Job)).scalar_one().status == "DEAD"
        assert s.execute(select(MessageLog)).scalar_one().status == "FAILED"


def test_timeout_unknown_never_resent(env):
    m = FakeMessenger(SendResult("UNKNOWN", error_code="timeout"))
    post(env[2], payload()); assert run(env, m) == "UNKNOWN"
    assert run(env, m) is None and len(m.calls) == 1


def test_default_catalog_for_unlisted_post(env, monkeypatch):
    import dataclasses
    cfg, _, c, S = env
    cfg2 = dataclasses.replace(cfg, default_catalog_id="c")
    m = FakeMessenger()
    post(c, payload("D1", post="UNLISTED"))
    jid = claim_job(S)
    assert process_job(S, cfg2, m, jid) == "SENT"
    assert "1.708.000đ" in m.calls[0][1]


def test_disabled_post_skipped_even_with_default(env):
    import dataclasses
    from app.db import FacebookPost
    cfg, _, c, S = env
    with S() as s, s.begin():
        s.add(FacebookPost(page_id="P1", post_id="OFF", enabled=False, catalog_id="c"))
    cfg2 = dataclasses.replace(cfg, default_catalog_id="c")
    post(c, payload("D2", post="OFF"))
    assert process_job(S, cfg2, FakeMessenger(), claim_job(S)) == "SKIPPED"


def test_post_info_cached_once(env):
    from app.db import PostInfo
    m = FakeMessenger()
    post(env[2], payload("E1")); run(env, m)
    post(env[2], payload("E2")); run(env, m)
    with env[3]() as s:
        rows = s.execute(select(PostInfo)).scalars().all()
        assert len(rows) == 1 and rows[0].message == "ban yem"


def test_admin_ui_and_webhook_detail(env):
    c = env[2]
    assert c.get("/admin").status_code == 401
    post(c, payload("A1"))
    post_bad = c.post("/webhooks/facebook", content=b"{}")
    assert post_bad.status_code == 403
    auth = ("admin", "pw")
    assert c.get("/admin", auth=auth).status_code == 200
    ov = c.get("/admin/api/overview", auth=auth).json()
    assert ov["counts"]["webhooks_ok"] == 1 and ov["counts"]["webhooks_bad"] == 1
    wid = [w for w in ov["webhooks"] if w["ok"]][0]["id"]
    d = c.get(f"/admin/api/webhook/{wid}", auth=auth).json()
    assert d["outcomes"][0]["result"] == "queued" and "x-hub-signature-256" in d["headers"]
    assert "A1" in d["processing"] and "comment_id" in d["body"]
    assert c.get("/admin/api/webhook/9999", auth=auth).status_code == 404
