# manager-order-facebook

# FB Page comment -> order -> Private Reply (BE)

Containers: `api` (FastAPI, GET/POST /webhooks/facebook), `worker` (queue consumer), `db` (Postgres, also the durable queue).

## Run
    cp .env.example .env            # fill secrets
    cp seed/seed.example.json seed/seed.json   # edit real post_id + catalog + prices
    docker compose up -d --build
Put existing HTTPS reverse proxy -> 127.0.0.1:${API_PORT:-8000}. Callback URL: https://<domain>/webhooks/facebook.
Tests: `docker build --target test .`   Local without Meta: `DRY_RUN=1`.

## Flow
POST -> verify X-Hub-Signature-256 (raw body, timing-safe) -> insert `jobs` (unique page:comment) -> 200.
Worker: claim (SKIP LOCKED) -> fetch comment if message/from missing -> filter page/self/post -> rule parser (prices from `catalog_items`) -> `orders` (unique per comment) -> `message_logs` (unique page,comment,action) atomic PENDING->SENDING -> POST /{comment}/private_replies.

## Status / errors
- order: READY | NEEDS_REVIEW (chấm/ib, missing/ambiguous size/color/qty, out of stock, no catalog)
- message_logs: PENDING | SENDING | SENT | FAILED | UNKNOWN
- 429/5xx/transient codes/connect error -> retry, exponential backoff, max MAX_ATTEMPTS -> FAILED/DEAD
- other 4xx (perm/policy) -> FAILED, no retry
- read timeout / crash mid-send -> UNKNOWN, never auto-resent; reconcile manually
- /metrics (status counters), /healthz

## Ops
- Token expired / App Review fail: sends become FAILED with Meta code in `message_logs.error_code`; fix token/permission, then reset row manually (`status='PENDING'`, job `PENDING`).
- Webhook down: Meta retries; dedupe makes replays safe.
- Default catalog: DEFAULT_CATALOG_ID applies to posts without a facebook_posts row; set enabled=false row to exclude a post.
- Per-post template: `facebook_posts.message_template` (uses `{lines}`, `{total}`).
- Verify with Meta docs before prod: feed webhook field, `private_replies` endpoint/permissions, Graph version.

## Facebook customer data

- Comment jobs read the author from `feed` first. If the ID, name, or comment text is missing, the worker calls `GET /{comment_id}?fields=id,message,from,created_time` with the Page token. Missing author data and permanent lookup errors leave the order flow active. Transient comment reads are retried with the existing backoff only when the comment text is also missing.
- Inbound Messenger messages use the same signed callback. The worker saves the sender PSID and tries `GET /{PSID}?fields=first_name,last_name,profile_pic`. A profile failure leaves the PSID and any email the customer sent saved. Send an email address as the entire message or use the format `Email: name@example.com`.
- Comment identities and PSIDs are scoped to the Page and stored separately. An inbound Messenger message links to an order when its Meta `message.reply_to.mid` matches the ID of a sent Private Reply for that order on the same Page. Messages without that reference are saved under the PSID and left unlinked.
- Email comes from the customer. The implemented profile requests provide no Facebook profile link, so `profile_url` stays null. The profile image URL can change and is refreshed on subsequent Messenger messages. New tables are created on startup. Existing customers are populated from newly processed events.

### Meta configuration and diagnostics

1. Call the authenticated `GET /admin/api/meta/token` to check whether `GET /me?fields=id` resolves to the configured Page. Meta code 190 indicates a token problem. Subcode 463 indicates expiry. A mismatched ID can mean a User Token or a Page token for another Page.
2. In Meta's [Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/), check token type `PAGE`, validity, associated Page, and the granted scopes or granular targets. Check `pages_read_engagement` and `pages_read_user_content` for the comment read, `pages_manage_metadata` for subscriptions, and `pages_messaging` for Messenger. In the App Dashboard, check App Review and the access level required for people outside the app's roles. Verify access with a real Page interaction even when the scopes are present, since Meta can return a successful response with author or profile fields omitted. The worker logs the operation, HTTP status, and Meta error codes for failed reads.
3. If Messenger events are absent, confirm that the existing Page webhook has both `feed` and `messages` enabled in the App Dashboard, and verify the Page subscription via the existing admin subscribe action or `python -m app.subscribe_page`. Use the existing callback URL. Test a comment and a Messenger reply from an approved test account, then from an account outside app roles when the app has the required access.
