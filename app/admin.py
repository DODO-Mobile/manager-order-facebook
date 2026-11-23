import secrets

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError

from .customers import actor
from .db import (FacebookCustomer, FacebookComment, Job, MessageLog, Order, PostInfo, WebhookEvent)
from .messenger import GraphReadError, Messenger
import os

basic = HTTPBasic(realm="fb-order-admin")
STATIC = os.path.join(os.path.dirname(__file__), "static", "admin.html")


def _iso(d):
    return d.isoformat() if d else None


def build_router(cfg, Session) -> APIRouter:
    r = APIRouter(prefix="/admin")
    messenger = Messenger(cfg)

    def auth(c: HTTPBasicCredentials = Depends(basic)):
        if not cfg.admin_password:
            raise HTTPException(503, "ADMIN_PASSWORD not set")
        if not (secrets.compare_digest(c.username, "admin")
                and secrets.compare_digest(c.password, cfg.admin_password)):
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Basic"})

    @r.get("", dependencies=[Depends(auth)])
    def page():
        return FileResponse(STATIC, media_type="text/html")

    @r.get("/api/overview", dependencies=[Depends(auth)])
    def overview():
        with Session() as s:
            def counts(col):
                return {k: n for k, n in s.execute(select(col, func.count()).group_by(col))}
            out = {
                "config": {"page_id": cfg.page_id, "graph_version": cfg.graph_version,
                           "dry_run": cfg.dry_run, "default_catalog": cfg.default_catalog_id,
                           "webhook_path": cfg.webhook_path},
                "counts": {"jobs": counts(Job.status), "orders": counts(Order.status),
                           "messages": counts(MessageLog.status),
                           "webhooks_ok": s.scalar(select(func.count()).select_from(WebhookEvent)
                                                   .where(WebhookEvent.signature_ok.is_(True))),
                           "webhooks_bad": s.scalar(select(func.count()).select_from(WebhookEvent)
                                                    .where(WebhookEvent.signature_ok.is_(False)))},
            }
            out["webhooks"] = [{"id": w.id, "at": _iso(w.received_at), "ok": w.signature_ok,
                                "comments": w.comment_events, "remote": w.remote,
                                "body": (w.body or "")[:300]}
                               for w in s.execute(select(WebhookEvent).order_by(WebhookEvent.id.desc()).limit(30)).scalars()]
            out["jobs"] = [{"id": j.id, "status": j.status, "attempts": j.attempts, "error": j.last_error,
                            "at": _iso(j.created_at), "comment_id": j.payload.get("comment_id"),
                            "message": j.payload.get("message")}
                           for j in s.execute(select(Job).order_by(Job.id.desc()).limit(30)).scalars()]
            orders = {(o.page_id, o.comment_id): o for o in s.execute(select(Order)).scalars()}
            logs = {(m.page_id, m.comment_id): m for m in s.execute(select(MessageLog)).scalars()}
            rows = []
            identity = case(
                (FacebookComment.user_id.is_not(None), "comment-user:" + FacebookComment.user_id),
                else_="comment:" + FacebookComment.comment_id,
            )
            comments = select(FacebookComment, FacebookCustomer.facebook_name).outerjoin(
                FacebookCustomer, (FacebookCustomer.page_id == FacebookComment.page_id)
                & (FacebookCustomer.identity_key == identity),
            ).order_by(FacebookComment.id.desc()).limit(50)
            for c, facebook_name in s.execute(comments):
                o, m = orders.get((c.page_id, c.comment_id)), logs.get((c.page_id, c.comment_id))
                rows.append({"comment_id": c.comment_id, "post_id": c.post_id, "user_id": c.user_id,
                             "content": c.content, "at": _iso(c.received_at), "facebook_name": facebook_name,
                             "order": {"status": o.status, "total": o.total, "reason": o.review_reason,
                                       "items": o.items_json} if o else None,
                             "message": {"status": m.status, "error": m.error_code, "detail": m.error_message,
                                         "meta_id": m.meta_message_id, "text": m.message_text} if m else None})
            out["comments"] = rows
            out["posts"] = [{"post_id": p.post_id, "message": p.message, "url": p.permalink_url}
                            for p in s.execute(select(PostInfo).order_by(PostInfo.id.desc()).limit(20)).scalars()]
        return out

    @r.get("/api/webhook/{wid}", dependencies=[Depends(auth)])
    def webhook_detail(wid: int):
        import json
        with Session() as s:
            w = s.get(WebhookEvent, wid)
            if not w:
                raise HTTPException(404, "not found")
            try:
                pretty = json.dumps(json.loads(w.body or ""), ensure_ascii=False, indent=2)
            except ValueError:
                pretty = w.body
            # processing result of each comment event in this delivery
            jobs = {}
            for ev in w.outcomes or []:
                j = s.execute(select(Job).where(Job.dedupe_key == f"{ev['page_id']}:{ev['comment_id']}")).scalar_one_or_none()
                o = s.execute(select(Order).where(Order.comment_id == ev["comment_id"])).scalar_one_or_none()
                m = s.execute(select(MessageLog).where(MessageLog.comment_id == ev["comment_id"])).scalar_one_or_none()
                jobs[ev["comment_id"]] = {
                    "job": {"status": j.status, "attempts": j.attempts, "error": j.last_error} if j else None,
                    "order": {"status": o.status, "total": o.total, "reason": o.review_reason, "items": o.items_json} if o else None,
                    "message": {"status": m.status, "error": m.error_code, "detail": m.error_message,
                                "meta_id": m.meta_message_id, "text": m.message_text} if m else None}
            return {"id": w.id, "at": _iso(w.received_at), "ok": w.signature_ok, "remote": w.remote,
                    "headers": w.headers, "body": pretty, "outcomes": w.outcomes or [], "processing": jobs}

    @r.get("/api/graph-scan", dependencies=[Depends(auth)])
    def graph_scan():
        """Live view from Graph: posts + comments, flagged if BE already received them."""
        try:
            data = messenger.scan(cfg.page_id)
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"[:300]}, status_code=502)
        if "error" in data:
            return JSONResponse({"error": data["error"].get("message", "graph error")}, status_code=502)
        with Session() as s:
            known = {c for (c,) in s.execute(select(FacebookComment.comment_id))}
        posts = []
        for p in data.get("data", []):
            cs = []
            for c in (p.get("comments") or {}).get("data", []):
                cs.append({"id": c["id"], "message": c.get("message"), "from": (c.get("from") or {}).get("id"),
                           "at": c.get("created_time"), "by_page": (c.get("from") or {}).get("id") == cfg.page_id,
                           "received": c["id"] in known})
            posts.append({"id": p["id"], "message": p.get("message"), "at": p.get("created_time"), "comments": cs})
        return {"posts": posts}

    @r.post("/api/import/{post_id}/{comment_id}", dependencies=[Depends(auth)])
    def import_comment(post_id: str, comment_id: str):
        """Manually push a Graph comment into the queue (same path as a webhook)."""
        try:
            data = messenger.fetch_comment(comment_id) or {}
        except GraphReadError as error:
            return JSONResponse({"error": str(error)}, status_code=502)
        from_id, from_name = actor(data.get("from"))
        ev = {"page_id": cfg.page_id, "comment_id": comment_id, "post_id": post_id,
              "message": data.get("message"), "from_id": from_id, "from_name": from_name}
        try:
            with Session() as s, s.begin():
                s.add(Job(dedupe_key=f"{cfg.page_id}:{comment_id}", correlation_id="admin-import", payload=ev))
        except IntegrityError:
            return {"status": "already_queued"}
        return {"status": "queued"}

    @r.get("/api/meta/token", dependencies=[Depends(auth)])
    def check_page_token():
        if cfg.dry_run:
            return {"status": "dry_run"}
        if not cfg.page_access_token or not cfg.page_id:
            return {"status": "missing_configuration"}
        try:
            token_page = messenger.token_page() or {}
        except GraphReadError as error:
            return JSONResponse({"status": "graph_error", "error": str(error)}, status_code=502)
        return {"status": "matches_page" if token_page.get("id") == cfg.page_id else "wrong_page_or_user_token",
                "permissions_to_check": {
                    "comment": ["pages_read_engagement", "pages_read_user_content"],
                    "webhook": ["pages_manage_metadata"],
                    "messenger": ["pages_messaging"],
                }}

    @r.post("/api/subscribe", dependencies=[Depends(auth)])
    def subscribe():
        return {"subscribe": messenger.subscribe_page(cfg.page_id),
                "current": messenger.list_subscriptions(cfg.page_id)}

    return r
