import json
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .customers import actor, supplied_email, text_value
from .db import Job, MessageLog, Order, WebhookEvent, create_schema, make_session_factory
from .security import verify_signature
from .seed import load_seed

log = logging.getLogger("api")


def _entries(payload):
    entries = payload.get("entry") if isinstance(payload, dict) else None
    return entries if isinstance(entries, list) else []


def extract_comment_events(payload: dict) -> list[dict]:
    """Tolerant extraction: only object=page, field=feed, item=comment, verb=add."""
    out = []
    if not isinstance(payload, dict) or payload.get("object") != "page":
        return out
    for entry in _entries(payload):
        if not isinstance(entry, dict):
            continue
        for ch in entry.get("changes") or []:
            if not isinstance(ch, dict) or ch.get("field") != "feed":
                continue
            v = ch.get("value")
            if not isinstance(v, dict) or v.get("item") != "comment" or v.get("verb") != "add" \
                    or not v.get("comment_id"):
                continue
            from_id, from_name = actor(v.get("from"))
            out.append({"page_id": str(entry.get("id", "")), "comment_id": str(v["comment_id"]),
                        "post_id": str(v["post_id"]) if v.get("post_id") else None,
                        "message": v.get("message") if isinstance(v.get("message"), str) else None,
                        "from_id": from_id, "from_name": from_name,
                        "parent_id": v.get("parent_id")})
    return out


def extract_messenger_events(payload, page_id):
    if not isinstance(payload, dict) or payload.get("object") != "page" or not page_id:
        return
    for entry in _entries(payload):
        if not isinstance(entry, dict) or str(entry.get("id")) != page_id:
            continue
        events = entry.get("messaging")
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            message = event.get("message")
            if not isinstance(message, dict) or message.get("is_echo"):
                continue
            psid, _ = actor(event.get("sender"))
            recipient, _ = actor(event.get("recipient"))
            mid = text_value(message.get("mid"))
            if not psid or len(psid) > 64 or psid == page_id or recipient != page_id or not mid or len(mid) > 128:
                continue
            reply = message.get("reply_to")
            yield {"kind": "messenger", "page_id": page_id, "psid": psid, "mid": mid,
                   "reply_mid": text_value(reply.get("mid")) if isinstance(reply, dict) else None,
                   "email": supplied_email(message.get("text"))}


KEEP_HEADERS = ("content-type", "content-length", "user-agent", "x-hub-signature-256",
                "x-hub-signature", "x-forwarded-for", "cf-connecting-ip", "cf-ray")


def _record(Session, request, ok: bool, raw: bytes, outcomes: list | None = None):
    try:
        hdrs = {k: v for k, v in request.headers.items() if k.lower() in KEEP_HEADERS}
        with Session() as s, s.begin():
            s.add(WebhookEvent(signature_ok=ok, comment_events=len(outcomes or []),
                               remote=request.headers.get("cf-connecting-ip") or
                               (request.client.host if request.client else None),
                               headers=hdrs, body=raw[:100_000].decode("utf-8", "replace"),
                               outcomes=outcomes or []))
    except Exception:
        log.exception("record webhook event failed")


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or Settings.load()
    engine, Session = make_session_factory(cfg.database_url)
    create_schema(engine)
    load_seed(Session, cfg.seed_file)
    app = FastAPI(docs_url=None, redoc_url=None)
    app.state.Session, app.state.cfg = Session, cfg

    @app.get(cfg.webhook_path)
    def verify(request: Request):
        q = request.query_params
        if (q.get("hub.mode") == "subscribe" and cfg.verify_token
                and q.get("hub.verify_token") == cfg.verify_token and q.get("hub.challenge")):
            return PlainTextResponse(q["hub.challenge"])
        return PlainTextResponse("forbidden", status_code=403)

    @app.post(cfg.webhook_path)
    async def receive(request: Request):
        raw = await request.body()
        ok = verify_signature(raw, request.headers.get("X-Hub-Signature-256"), cfg.app_secret)
        if not ok:
            _record(Session, request, False, b"")
            return PlainTextResponse("forbidden", status_code=403)
        try:
            payload = json.loads(raw)
        except ValueError:
            _record(Session, request, True, raw)
            return PlainTextResponse("ok")  # signed but junk: ack, don't make Meta retry
        cid = uuid.uuid4().hex[:12]
        outcomes = []
        for ev in extract_comment_events(payload):
            key = f"{ev['page_id']}:{ev['comment_id']}"
            try:
                with Session() as s, s.begin():
                    s.add(Job(dedupe_key=key, correlation_id=cid, payload=ev))
                outcomes.append({**ev, "result": "queued"})
            except IntegrityError:
                log.info("[cid=%s comment=%s] duplicate webhook ignored", cid, ev["comment_id"])
                outcomes.append({**ev, "result": "duplicate"})
        for ev in extract_messenger_events(payload, cfg.page_id):
            try:
                with Session() as s, s.begin():
                    s.add(Job(dedupe_key=f"messenger:{ev['page_id']}:{ev['mid']}",
                              correlation_id=cid, payload=ev))
            except IntegrityError:
                log.info("[cid=%s] duplicate Messenger webhook ignored", cid)

        # Keep feed diagnostics without copying private conversations into the webhook viewer.
        if any(isinstance(entry, dict) and "messaging" in entry for entry in _entries(payload)):
            raw = json.dumps({**payload, "entry": [
                {key: value for key, value in entry.items() if key != "messaging"}
                for entry in _entries(payload) if isinstance(entry, dict)
            ]}, ensure_ascii=False).encode()
        _record(Session, request, True, raw, outcomes)
        return PlainTextResponse("ok")

    @app.get("/healthz")
    def health():
        with Session() as s:
            s.execute(select(1))
        return {"ok": True}

    @app.get("/metrics")
    def metrics():
        lines = []
        with Session() as s:
            for name, col, model in (("jobs", Job.status, Job), ("orders", Order.status, Order),
                                     ("messages", MessageLog.status, MessageLog)):
                for st, n in s.execute(select(col, func.count()).group_by(col)):
                    lines.append(f'fb_{name}_total{{status="{st}"}} {n}')
        return PlainTextResponse("\n".join(lines) + "\n")

    from .admin import build_router
    app.include_router(build_router(cfg, Session))
    return app
