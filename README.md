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
