# Order Tracker (Docker, self-hosted)

Flask + SQLite backend, a single-file frontend, a `workflow.json` that defines the
stages, and a small deploy webhook for CI/CD. No build step - everything is
editable live.

```
app.py                      backend (API, SQLite, webhook, Telegram trigger)
workflow.json                the 13 stages: titles, fields, who we wait on, where each is checked
static/index.html            the whole UI
tg_group.py / tg_login.py    creates the Telegram group for a new order (Telethon)
Dockerfile / docker-compose.yml / .dockerignore    runs the app in Docker
deploy/deploy.py             deploy webhook: git pull + docker compose up -d --build
deploy/deploy.service        systemd unit for the deploy webhook (runs on the VM directly, not in Docker)
n8n-mcp-deploy.workflow.ts   n8n Workflow SDK code: exposes the deploy webhook as MCP tools
.env.example                 settings (copy to .env)
order-tracker.service        systemd unit that runs `docker compose up -d` on boot
Caddyfile                    reverse proxy + basic auth + automatic HTTPS
```

## 1. Install on the VM (Debian, Docker)

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin git
sudo useradd -r -m -d /opt/order-tracker tracker
sudo usermod -aG docker tracker                    # tracker needs the docker group for deploy.py
sudo mkdir -p /opt/order-tracker && sudo chown tracker: /opt/order-tracker

# as the tracker user (su - tracker, or set up a deploy key first):
cd /opt/order-tracker
git clone <your-repo-url> .                        # or: copy this folder's contents here
cp .env.example .env && nano .env                   # set HOOK_TOKEN, DEPLOY_TOKEN, ERP_URL_TEMPLATE ...

docker compose build
docker compose up -d
docker compose exec app curl -fsS localhost:8080/healthz

sudo cp order-tracker.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable order-tracker
```

The app publishes no host port at all - it's reachable only from other containers on the
`n8n-stack_default` network (see `docker-compose.yml`), which is what lets your existing
containerized Caddy reach it without exposing anything new. Section 8 covers wiring that up.

I don't have a Debian+Docker environment to build and run this in from here, so treat the
first `docker compose up -d` as the real test: watch `docker compose logs -f app` and confirm
`docker compose exec app curl -fsS localhost:8080/healthz` responds before relying on it. The
`networks.proxy.name` in `docker-compose.yml` is a best guess (`n8n-stack_default`, from your
`docker ps` container names) - confirm it with
`docker inspect n8n-stack-caddy-1 --format '{{json .NetworkSettings.Networks}}'` and fix the
compose file if it's actually named something else.

## 2. Editing it live

Same idea as running it bare - the difference is `docker-compose.yml` bind-mounts the whole
project directory into the container (`.:/app`), so the container's files ARE the files on disk:

| You edit | What happens |
|---|---|
| `workflow.json` (stages, fields, hints, channels, colours) | picked up by every open browser within ~4 s. No restart. Invalid JSON: browsers keep the last good version and show the error. |
| `static/index.html` | open browsers reload themselves within ~4 s (they wait until nobody is typing). |
| `app.py`, `tg_group.py` | gunicorn `--reload` (baked into the image's `CMD`) restarts the worker inside the container automatically - no `docker compose restart` needed. |
| `requirements.txt`, `Dockerfile` | needs an image rebuild: `docker compose up -d --build`. |
| `.env` | `docker compose up -d` (recreates the container with the new env). |

Work in the folder over VS Code Remote-SSH; `git init` it (if you didn't clone it) so you can
roll back a bad edit; validate before saving with `python3 -m json.tool workflow.json`. Orders
live in `data/tracker.db` and are untouched by any of this.

Adding a stage = add an object to `steps` in `workflow.json` (new `n`, `requires`, `channel`,
`fields`). Field types: `text`, `date`, `datetime`, `check`.

## 3. Where each stage is checked

Set per stage in `workflow.json` (`"channel"`), shown on every stage card:

- **ERP portal**: 1, 2. "Open in ERP" button when `ERP_URL_TEMPLATE` is set and the order has an ERP reference.
- **Email**: 3, 4, 5, 6, 9, 12, 13. "Copy subject tag" button copies `[ORD-260921-ABC]` - put that in every email subject and the thread is a search away.
- **Telegram group**: 7, 8, 10, 11. "Open group" button using the order's group link.

## 4. Telegram group per order

A Telegram **bot cannot create groups** - this uses a real user account via Telethon.

1. Use a dedicated company Telegram account.
2. Get `api_id` / `api_hash` at https://my.telegram.org -> API development tools. Put them in
   `.env` with `TG_ENABLED=true`, list the team in `TG_ADD_USERS`.
3. Log the account in once (session file needs to land in the bind-mounted `data/` folder so
   it survives rebuilds):
   `docker compose run --rm app python tg_login.py`
4. `docker compose up -d`

On order creation the backend creates a supergroup `{customer} | {id}`, adds `TG_ADD_USERS` (and
the customer's @username if given), posts a welcome message, and stores the invite link on the
order - shown with status, Open, Copy invite link, and Retry on failure. Customer auto-add only
works if you gave their @username and their privacy settings allow it; otherwise share the link.
Keep `data/tg.session` private - it's a login for that account. Don't bulk-import old orders with
`TG_ENABLED=true` (Telegram rate-limits group creation).

## 5. Start an order from n8n / the ERP

```bash
curl -X POST https://tracker.example.internal/api/hook/order \
  -H "X-Api-Token: $HOOK_TOKEN" -H "Content-Type: application/json" \
  -d '{"customer":"Acme Networks","contact":"noc@acme.example","service":"100 Mbps ILL",
       "telco":"Telco A","erp_ref":"SO-1042","payref":"UTR123","tg_user":"@acme_noc","site":"Indore"}'
```

Only `customer` is required. `WEBHOOK_URL` (in `.env`) makes the tracker POST
`{"event": "order.created"|"step.done"|"step.reopened", "step": "10", "order": {...}}` back to an
n8n webhook - a good place for reminders, or the NetBox/router config push when stage 10 starts.

## 6. CI/CD: redeploy over MCP

`deploy/deploy.py` is a small, dependency-free webhook that runs **directly on the VM** (not in
Docker - it needs to call `git` and `docker compose` with normal access, so it avoids
docker-socket-mounting complexity):

```
POST /deploy          git fetch + reset --hard origin/<branch>, then docker compose up -d --build
                       Header: Authorization: Bearer $DEPLOY_TOKEN
                       -> 202 immediately (the build runs in the background); 409 if one is already running
GET  /deploy/status    {"state": "idle|running|done|failed", "exit_code", "commit", "log_tail", ...}
```

Set it up:

```bash
# DEPLOY_TOKEN is already in .env from step 1
sudo cp deploy/deploy.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now deploy
curl -H "Authorization: Bearer $DEPLOY_TOKEN" localhost:8091/deploy/status
```

Three ways to trigger it, pick any (or all) that fit how you work:

- **By hand / from a script:** `curl -X POST -H "Authorization: Bearer $DEPLOY_TOKEN" https://tracker.example.internal/deploy`
- **GitHub Actions on push:** add a step that runs the same curl against your VM (over your
  VPN, or the `Caddyfile`'s `/deploy` route) after tests pass.
- **MCP, so you can just ask Claude to redeploy it:** `n8n-mcp-deploy.workflow.ts` wires an n8n
  **MCP Server Trigger** to two tools - *Redeploy Order Tracker* (calls `POST /deploy`) and
  *Order Tracker Deploy Status* (calls `GET /deploy/status`) - backed by this same webhook. I
  validated that file with n8n's own workflow validator, but the n8n connector in this session
  only exposes read/validate tools, not one that creates workflows, so I couldn't import it for
  you. The file has the exact steps: paste the code into n8n's AI Workflow Builder if it's
  available on your instance, or recreate the 3 nodes by hand from the shapes in the file (an
  MCP Server Trigger with Bearer auth, and two HTTP Request tool nodes pointed at `/deploy` and
  `/deploy/status`). For the URL in both tools, use the public hostname from your Caddyfile
  (e.g. `https://tracker.yourdomain.com/deploy`) - your n8n container reaches that over the
  open internet like any other client, so it needs no special network access to the VM. Once
  it's running and you connect an MCP client (Claude Code, Claude Desktop, ...) to that n8n
  endpoint, "redeploy the order tracker" is enough - Claude calls the tool, then checks status
  before telling you it's done.

Notes:
- Only two fixed command lines ever run (`git ...`, `docker compose ...`) - nothing from the
  request is put into a shell command.
- A deploy already running gets a 409 rather than a second, overlapping run.
- `deploy/deploy.py` needs no pip install (standard library only) - just Python 3 and the
  `tracker` user in the `docker` group.

## 7. Notes

- Stage order (`requires`) is enforced by the UI, not the API - fine for an internal tool, but
  the API accepts a manual PATCH that skips ahead.
- Single gunicorn worker (`-w 1` in the `Dockerfile`): one Telegram session file, kept simple.
- Back up `data/tracker.db`, `.env`, and `data/tg.session` (e.g.
  `sqlite3 data/tracker.db ".backup '/backups/tracker-$(date +%F).db'"`).
- Updates are near-live (4 s polling); two people editing the same field at once is last-write-wins.

## 8. Putting it on a public IP (via your existing containerized Caddy)

Your Caddy isn't a host-installed binary - it's `n8n-stack-caddy-1`, already bound to
`0.0.0.0:80`/`0.0.0.0:443` as part of the n8n stack. So this isn't a new install: it's one
network change to that stack, plus a new site block in the Caddyfile it already uses.

**1. Put the app on the same Docker network as that Caddy container** - `docker-compose.yml`
already does this (`networks.proxy`, external, pointed at `n8n-stack_default`). No host port
is published for the app at all; Caddy reaches it as `order-tracker-app` by container name.
Confirm the network name matches yours (see the note in section 1) before `docker compose up -d`.

**2. Let that Caddy container reach the deploy webhook on the host.** `deploy/deploy.py` has to
stay outside Docker (it needs plain `git`/`docker compose` access), so unlike the app, Caddy
can't reach it by container name - it needs `host.docker.internal`, which only resolves inside
the container once the **n8n-stack's own** `docker-compose.yml` (a different file, wherever that
stack lives - `docker inspect n8n-stack-caddy-1 --format '{{ index .Config.Labels
"com.docker.compose.project.working_dir" }}'` finds it) has this on the `caddy` service:

```yaml
services:
  caddy:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

And `deploy.py` itself needs to be listening on more than loopback - add `DEPLOY_HOST=0.0.0.0`
to order-tracker's `.env` (already commented in `.env.example`). It's still gated by
`DEPLOY_TOKEN` on every request with no fallback/trust path, so this is a reasonable trade; for
something tighter, bind it to the docker bridge gateway IP instead of `0.0.0.0` - find it with
`docker network inspect n8n-stack_default --format '{{(index .IPAM.Config 0).Gateway}}'`.

**3. Add the site block.** `Caddyfile` in this repo is written as a snippet, not a standalone
config - open the n8n-stack Caddyfile (`docker inspect n8n-stack-caddy-1 --format
'{{json .Mounts}}'` shows its host path) and add that block to it, alongside whatever block
already serves n8n. Generate the password hash from inside the running container so the version
always matches: `docker exec n8n-stack-caddy-1 caddy hash-password --plaintext 'a-real-password'`.

**4. Apply both changes together** - the `extra_hosts` addition needs the container recreated,
which also picks up the new Caddyfile, so one command handles both:

```bash
cd <n8n-stack directory>
docker exec n8n-stack-caddy-1 caddy validate --config /etc/caddy/Caddyfile   # path from step 3
docker compose up -d caddy
```

`/api/hook/*` and `/deploy*` are routed around the UI's basic auth in the Caddyfile (they're
protected by `HOOK_TOKEN` / `DEPLOY_TOKEN` instead), and the UI's basic-auth username is
forwarded to the app as `X-Remote-User`. I validated the Caddyfile's syntax and directive names
against the current Caddy release, and ran it end-to-end against stand-ins for the app and the
deploy webhook to confirm the routing, auth, and header forwarding all behave as described - the
one thing I couldn't test from here is the actual container-name and `host.docker.internal`
resolution on your VM, since that depends on your real n8n-stack network.
