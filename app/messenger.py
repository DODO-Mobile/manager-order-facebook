"""Meta Graph adapter. Verify endpoint/permissions against current Meta docs before production."""
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("messenger")

RETRYABLE_CODES = {1, 2, 4, 17, 32, 341, 613}   # transient / rate limit families


@dataclass
class SendResult:
    outcome: str            # SENT | RETRY | FAILED | UNKNOWN
    message_id: str | None = None
    error_code: str | None = None
    error_subcode: str | None = None
    error_message: str | None = None


class Messenger:
    def __init__(self, settings, client: httpx.Client | None = None):
        self.s = settings
        self.base = f"https://graph.facebook.com/{settings.graph_version}"
        self.client = client or httpx.Client(timeout=settings.send_timeout)
        self.headers = {"Authorization": f"Bearer {settings.page_access_token}"}

    def fetch_comment(self, comment_id: str) -> dict | None:
        if self.s.dry_run:
            return None
        r = self.client.get(f"{self.base}/{comment_id}", headers=self.headers,
                            params={"fields": "message,from,parent"})
        r.raise_for_status()
        return r.json()

    def fetch_post(self, post_id: str) -> dict | None:
        if self.s.dry_run:
            return None
        r = self.client.get(f"{self.base}/{post_id}", headers=self.headers,
                            params={"fields": "message,permalink_url,created_time"})
        r.raise_for_status()
        return r.json()

    def scan(self, page_id: str, limit: int = 10) -> dict:
        r = self.client.get(f"{self.base}/{page_id}/posts", headers=self.headers, params={
            "fields": "id,created_time,message,comments.limit(25){id,message,from,created_time}",
            "limit": limit})
        return r.json()

    def subscribe_page(self, page_id: str, fields: str = "feed") -> dict:
        r = self.client.post(f"{self.base}/{page_id}/subscribed_apps", headers=self.headers,
                             data={"subscribed_fields": fields})
        return {"status": r.status_code, "body": r.json() if r.content else {}}

    def list_subscriptions(self, page_id: str) -> dict:
        r = self.client.get(f"{self.base}/{page_id}/subscribed_apps", headers=self.headers)
        return {"status": r.status_code, "body": r.json() if r.content else {}}

    def private_reply(self, comment_id: str, message: str) -> SendResult:
        if self.s.dry_run:
            log.info("DRY_RUN private_reply comment=%s", comment_id)
            return SendResult("SENT", message_id=f"dryrun-{comment_id}")
        try:
            r = self.client.post(f"{self.base}/{comment_id}/private_replies",
                                 headers=self.headers, json={"message": message})
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return SendResult("RETRY", error_code="network", error_message="connect failed")  # not sent
        except httpx.TimeoutException as e:       # read/write timeout: may have been delivered
            return SendResult("UNKNOWN", error_code="timeout", error_message=str(e)[:200])
        except httpx.HTTPError as e:
            return SendResult("UNKNOWN", error_code="transport", error_message=str(e)[:200])

        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code < 300:
            mid = body.get("message_id") or body.get("id")
            if mid:
                return SendResult("SENT", message_id=str(mid))
            return SendResult("UNKNOWN", error_code="no_id", error_message="2xx without id")
        err = body.get("error", {}) if isinstance(body, dict) else {}
        code, sub = err.get("code"), err.get("error_subcode")
        res = SendResult("FAILED", error_code=str(code or r.status_code),
                         error_subcode=str(sub) if sub else None,
                         error_message=str(err.get("message", ""))[:300])
        if r.status_code == 429 or r.status_code >= 500 or code in RETRYABLE_CODES:
            res.outcome = "RETRY"
        return res
