import datetime as dt
import logging
import time

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from . import templates
from .config import Settings
from .db import (PostInfo, CatalogItem, FacebookComment, FacebookPost, Job, MessageLog, Order, utcnow)
from .messenger import Messenger
from .parser import PARSER_VERSION, parse_comment

log = logging.getLogger("worker")
STALE_LOCK = dt.timedelta(minutes=5)
ACTION = "private_reply"


def claim_job(Session) -> int | None:
    """Atomic claim: SKIP LOCKED on Postgres; also recovers stale RUNNING jobs."""
    with Session() as s, s.begin():
        s.execute(update(Job).where(Job.status == "RUNNING", Job.locked_at < utcnow() - STALE_LOCK)
                  .values(status="PENDING"))
        job = s.execute(select(Job).where(Job.status == "PENDING", Job.next_run_at <= utcnow())
                        .order_by(Job.id).limit(1).with_for_update(skip_locked=True)).scalar_one_or_none()
        if not job:
            return None
        job.status, job.locked_at, job.attempts = "RUNNING", utcnow(), job.attempts + 1
        return job.id


def _cache_post(Session, messenger, page_id, post_id, tag):
    """Best-effort: store post text once per post_id. Never blocks order flow."""
    if not post_id:
        return
    try:
        with Session() as s:
            if s.execute(select(PostInfo.id).where(PostInfo.page_id == page_id,
                                                   PostInfo.post_id == post_id)).first():
                return
        data = messenger.fetch_post(post_id)
        if not data:
            return
        with Session() as s, s.begin():
            s.add(PostInfo(page_id=page_id, post_id=post_id, message=data.get("message"),
                           permalink_url=data.get("permalink_url")))
    except Exception as e:
        log.warning("%s fetch_post failed: %s", tag, type(e).__name__)


def _finish(Session, job_id, status, error=None, retry_in=None):
    with Session() as s, s.begin():
        j = s.get(Job, job_id)
        j.status, j.last_error, j.locked_at = status, error, None
        if retry_in is not None:
            j.status, j.next_run_at = "PENDING", utcnow() + dt.timedelta(seconds=retry_in)


def _backoff(cfg: Settings, attempt: int) -> int:
    return min(cfg.backoff_base_seconds * 2 ** (attempt - 1), 3600)


def process_job(Session, cfg: Settings, messenger: Messenger, job_id: int) -> str:
    with Session() as s:
        job = s.get(Job, job_id)
        p, cid, attempts = job.payload, job.correlation_id, job.attempts
    page_id, comment_id = p["page_id"], p["comment_id"]
    tag = f"[cid={cid} comment={comment_id}]"

    _cache_post(Session, messenger, page_id, p.get("post_id"), tag)

    # 1. fill missing fields from Graph (payload may lack message/from)
    message, user_id = p.get("message"), p.get("from_id")
    if message is None or user_id is None:
        try:
            data = messenger.fetch_comment(comment_id) or {}
            message = message if message is not None else data.get("message")
            user_id = user_id or (data.get("from") or {}).get("id")
        except Exception as e:
            log.warning("%s fetch_comment failed: %s", tag, type(e).__name__)
            if attempts >= cfg.max_attempts:
                _finish(Session, job_id, "DEAD", f"fetch:{type(e).__name__}")
                return "DEAD"
            _finish(Session, job_id, "PENDING", f"fetch:{type(e).__name__}", _backoff(cfg, attempts))
            return "RETRY"

    with Session() as s, s.begin():
        post_id = p.get("post_id")
        if not s.execute(select(FacebookComment.id).where(
                FacebookComment.page_id == page_id, FacebookComment.comment_id == comment_id)).first():
            s.add(FacebookComment(page_id=page_id, comment_id=comment_id, post_id=post_id,
                                  user_id=user_id, content=message))
        # 2. filters
        if page_id != cfg.page_id or (user_id and user_id == cfg.page_id):
            skip = "page_not_allowed_or_self_comment"
        else:
            post = s.execute(select(FacebookPost).where(
                FacebookPost.page_id == page_id, FacebookPost.post_id == post_id)).scalar_one_or_none()
            if post is not None and not post.enabled:
                skip = "post_disabled"
            elif post is None and not cfg.default_catalog_id:
                skip = "post_not_configured"
            else:
                skip = None
        if skip:
            log.info("%s skipped: %s", tag, skip)
            _finish_in(s, job_id, "DONE", skip)
            return "SKIPPED"
        catalog_id = (post.catalog_id if post and post.catalog_id else cfg.default_catalog_id)
        catalog = list(s.execute(select(CatalogItem).where(
            CatalogItem.catalog_id == catalog_id)).scalars()) if catalog_id else []
        tpl = post.message_template if post else None

        # 3. order (unique per comment)
        order = s.execute(select(Order).where(Order.page_id == page_id,
                                              Order.comment_id == comment_id)).scalar_one_or_none()
        if not order:
            r = parse_comment(message, catalog, cfg.currency)
            order = Order(page_id=page_id, comment_id=comment_id,
                          items_json=[i.__dict__ for i in r.items], total=r.total,
                          currency=r.currency, status=r.status, review_reason=r.reason,
                          confidence=r.confidence, parser_version=PARSER_VERSION)
            s.add(order)
        items, total, order_status = order.items_json, order.total, order.status
    if order_status != "READY":
        log.info("%s order NEEDS_REVIEW", tag)
        _finish(Session, job_id, "DONE", "needs_review")
        return "NEEDS_REVIEW"

    # 4. idempotency row
    with Session() as s:
        try:
            with s.begin():
                s.add(MessageLog(page_id=page_id, comment_id=comment_id, action=ACTION,
                                 template_version=templates.TEMPLATE_VERSION,
                                 message_text=templates.render(items, total, tpl)))
        except IntegrityError:
            pass
    # 5. atomic claim -> SENDING
    with Session() as s, s.begin():
        ml = s.execute(select(MessageLog).where(
            MessageLog.page_id == page_id, MessageLog.comment_id == comment_id,
            MessageLog.action == ACTION)).scalar_one()
        if ml.status == "SENDING":      # crashed mid-send: outcome unknown, never resend blindly
            ml.status, ml.error_code = "UNKNOWN", "crash_during_send"
            _finish_in(s, job_id, "DONE", "unknown_needs_reconcile")
            return "UNKNOWN"
        if ml.status in ("SENT", "FAILED", "UNKNOWN"):
            _finish_in(s, job_id, "DONE", f"already_{ml.status.lower()}")
            return "DUPLICATE"
        claimed = s.execute(update(MessageLog).where(MessageLog.id == ml.id, MessageLog.status == "PENDING")
                            .values(status="SENDING")).rowcount
        text = ml.message_text
        ml_id = ml.id
    if claimed != 1:
        _finish(Session, job_id, "DONE", "claimed_elsewhere")
        return "DUPLICATE"

    # 6. send (outside txn)
    res = messenger.private_reply(comment_id, text)
    with Session() as s, s.begin():
        ml = s.get(MessageLog, ml_id)
        ml.retry_count = attempts - 1
        ml.error_code, ml.error_subcode, ml.error_message = res.error_code, res.error_subcode, res.error_message
        if res.outcome == "SENT":
            ml.status, ml.meta_message_id, ml.sent_at = "SENT", res.message_id, utcnow()
            _finish_in(s, job_id, "DONE", None)
        elif res.outcome == "RETRY" and attempts < cfg.max_attempts:
            ml.status = "PENDING"
            _finish_in(s, job_id, "PENDING", f"retry:{res.error_code}", _backoff(cfg, attempts))
        elif res.outcome == "UNKNOWN":
            ml.status = "UNKNOWN"
            _finish_in(s, job_id, "DONE", "unknown_needs_reconcile")
        else:
            ml.status = "FAILED"
            _finish_in(s, job_id, "DONE" if res.outcome == "FAILED" else "DEAD", f"failed:{res.error_code}")
    log.info("%s send outcome=%s code=%s", tag, res.outcome, res.error_code)
    return res.outcome


def _finish_in(s, job_id, status, error, retry_in=None):
    j = s.get(Job, job_id)
    j.status, j.last_error, j.locked_at = status, error, None
    if retry_in is not None:
        j.next_run_at = utcnow() + dt.timedelta(seconds=retry_in)


def run_forever(Session, cfg: Settings, messenger: Messenger):
    log.info("worker started")
    while True:
        jid = claim_job(Session)
        if jid is None:
            time.sleep(cfg.poll_interval)
            continue
        try:
            process_job(Session, cfg, messenger, jid)
        except Exception as e:  # never kill loop; job returns via stale-lock recovery
            log.exception("job %s crashed: %s", jid, type(e).__name__)
            _finish(Session, jid, "PENDING", f"crash:{type(e).__name__}", 30)
