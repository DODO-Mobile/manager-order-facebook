"""Meta Graph adapter. Verify endpoint/permissions against current Meta docs before production."""
import logging
from dataclasses import dataclass

import httpx
from urllib.parse import quote

log = logging.getLogger("messenger")

RETRYABLE_CODES = {1, 2, 4, 17, 32, 341, 613}   # transient / rate limit families


class GraphReadError(Exception):
    def __init__(self, endpoint, status=None, code=None, subcode=None, reason=None):
        if reason is None:
            if code == 190 or status == 401:
                reason = "token_expired" if subcode == 463 else "token_invalid"
            elif status == 429 or code in RETRYABLE_CODES | {80001, 80006}:
                reason = "rate_limit_or_transient"
            elif status == 403 or code in {10, 200, 230, 283}:
                reason = "permission_or_access"
            elif status >= 500:
                reason = "upstream"
            else:
                reason = "graph_error"
        self.retryable = reason in {"network", "rate_limit_or_transient", "upstream"}
        super().__init__(f"{endpoint} reason={reason} http={status} meta_code={code} meta_subcode={subcode}")


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

    def _read(self, object_id, fields, endpoint):
        if self.s.dry_run:
            return None
        try:
            response = self.client.get(f"{self.base}/{quote(object_id, safe='')}", headers=self.headers,
                                       params={"fields": fields})
        except httpx.RequestError:
            raise GraphReadError(endpoint, reason="network") from None
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if not response.is_success or error is not None or not isinstance(body, dict):
            error = error if isinstance(error, dict) else {}
            code, subcode = error.get("code"), error.get("error_subcode")
            raise GraphReadError(endpoint, response.status_code,
                                 code if type(code) is int else None,
                                 subcode if type(subcode) is int else None)
        return body

    def fetch_comment(self, comment_id: str) -> dict | None:
        return self._read(comment_id, "id,message,from,created_time",
                          "GET /{comment_id} [pages_read_engagement,pages_read_user_content]")

    def fetch_profile(self, psid: str) -> dict | None:
        return self._read(psid, "first_name,last_name,profile_pic",
                          "GET /{PSID} [pages_messaging]")

    def token_page(self) -> dict | None:
        return self._read("me", "id", "GET /me")

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

    def subscribe_page(self, page_id: str, fields: str = "feed,messages") -> dict:
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
