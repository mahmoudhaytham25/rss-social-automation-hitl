# Setup: Running These Workflows on Your Own n8n

This guide gets the RSS → Gemini → Discord-approval → multi-platform posting
pipeline running on a fresh n8n instance.

## Prerequisites

- **Option A (recommended):** Docker with the Compose plugin (`docker compose
  version`). The included `docker-compose.yml` pins a tested n8n 1.x image.
- **Option B:** An existing n8n instance **>= 1.75** — required for the Discord
  `sendAndWait` node's `limitWaitTime` option used by the approval nodes. Skip
  step 1 and import via the CLI on that instance (or the UI — see the id
  warning in step 2).
- Accounts you will connect during credential setup: a Google AI Studio
  (Gemini) API key, a Discord application/bot, a Facebook Page access token,
  a Bluesky account, and a LinkedIn account/app (details below).

## Step 1 — Start n8n

```bash
cp .env.example .env   # fill in the values (see comments in the file)
docker compose up -d
```

Minimum edits in `.env`:

- `N8N_ENCRYPTION_KEY` — generate once: `openssl rand -hex 32`. **Do not
  change it later** — it encrypts stored credentials, and changing it makes
  them all unreadable.
- `WEBHOOK_URL` — leave as `http://localhost:5678/` for local-only use. The
  Discord `sendAndWait` approval links are built from this URL, so set it to
  your public n8n URL if you want to approve posts from another device (see
  [Remote approvals](#remote-approvals-approving-from-your-phone) below).

Open http://localhost:5678 and create the n8n owner account on first launch.

## Step 2 — Import the workflows (preserve workflow ids)

**Import order matters.** `main.json` calls the other workflows through
`executeWorkflow` nodes that reference workflow **ids**, and every export
references the error handler through `settings.errorWorkflow`. Importing
through the n8n **CLI preserves the ids embedded in the export files** — so
import with the CLI, and import the sub-workflows and the error handler
before `main.json`:

```bash
docker compose exec n8n n8n import:workflow --input=/files/workflows/error-handler.json
docker compose exec n8n n8n import:workflow --input=/files/workflows/post-creation-flow.json
docker compose exec n8n n8n import:workflow --input=/files/workflows/bluesky-branch.json
docker compose exec n8n n8n import:workflow --input=/files/workflows/facebook-branch.json
docker compose exec n8n n8n import:workflow --input=/files/workflows/linkedin-branch.json
docker compose exec n8n n8n import:workflow --input=/files/workflows/main.json
```

Or as a one-liner:

```bash
for f in error-handler post-creation-flow bluesky-branch facebook-branch linkedin-branch main; do
  docker compose exec n8n n8n import:workflow --input="/files/workflows/${f}.json"
done
```

(The `./workflows` directory is bind-mounted read-only inside the container
at `/files/workflows`. On a non-Docker n8n, copy the JSON files to the host
and run `n8n import:workflow --input=<path>` directly.)

Expected ids after import — verify they match what `main.json` references:

| File | Workflow name | Workflow id |
|------|---------------|-------------|
| `error-handler.json` | Error handler | *(id embedded in the file)* |
| `post-creation-flow.json` | Post creation flow | `9WUvqETxyFrEav7H` |
| `bluesky-branch.json` | Bluesky branch | `zvxqmMaPcqiBRWOC` |
| `facebook-branch.json` | Facebook branch | `rYRy7pbxfnStYz7H` |
| `linkedin-branch.json` | Linkedin branch | `q2qKF9EkXT8XnINX` |
| `main.json` | Project 3 | `E5LNKyY3Odkk92b1` |

### If you import through the n8n UI instead

The UI generates **new** workflow ids, so every internal reference must be
re-pointed manually afterwards:

- In `main.json`, re-select the target workflow in all **6 `executeWorkflow`
  nodes**: `Create Facebook Post`, `Create Bluesky Post`, `Create Linkedin
  Post` (all → post-creation-flow), and `Post to Facebook`, `Post to
  Bluesky`, `Post to Linkedin` (→ the matching branch workflow).
- In **all 5** non-error workflows, open **Settings → Error workflow** and
  re-select the error handler workflow.

## Step 3 — Create the credentials

The exports reference credential ids that do not exist on your instance, so
each node needs a new credential attached after import (click the node →
Credential → Create new). Five credential types are used:

| n8n credential type | Used in | How to create |
|---------------------|---------|---------------|
| `googlePalmApi` — "Google PaLM API" | `post-creation-flow.json` → **Google Gemini Chat Model** (LLM behind the AI Agent) | Create a Gemini API key at [Google AI Studio](https://aistudio.google.com/apikey). In n8n: **Credentials → New → Google PaLM API** → paste the key. |
| `discordOAuth2Api` | `main.json` → all 5 Discord nodes (3 `sendAndWait` approvals + 2 separator messages); `error-handler.json` → the failure-notification node | Create an application in the [Discord Developer Portal](https://discord.com/developers/applications/), enable the **bot** and invite it to your server with permission to view + post in the approval channel. Under **OAuth2 → Redirects**, add the OAuth Redirect URL shown in n8n's Discord credential screen (use the public `WEBHOOK_URL` when approving remotely — see the [ngrok guide](ngrok-approval-setup.md)). Then connect the credential in n8n. |
| `httpBearerAuth` — "Header Auth" / Bearer token | `facebook-branch.json` → all 4 HTTP nodes (`Get Facebook Page ID`, `Image upload to FB`, `Post Creation FB`, `Post Creation FB1`) — Graph API `v23.0` | Generate a **Facebook Page access token** in Meta for Developers (`pages_manage_posts`, `pages_read_engagement` scopes). In n8n: **Credentials → New → Bearer Auth (HTTP)** → paste the token. |
| `httpCustomAuth` — "Custom Auth" | `bluesky-branch.json` → **Open BlueSky Session** (`POST https://bsky.social/xrpc/com.atproto.server.createSession`) | The node sends the credential's JSON as the request body to `createSession`. In n8n: **Credentials → New → Custom Auth** and set the JSON to `{"body": {"identifier": "<your-handle>.bsky.social", "password": "<app-password>"}}`. Create the app password at https://bsky.social/settings/app-passwords — do **not** use your main password. The node already sends `Content-Type: application/json`. |
| `linkedInOAuth2Api` | `linkedin-branch.json` → **Create a post WO/img**, **Create a post W/img** | n8n's built-in LinkedIn OAuth2: **Credentials → New → LinkedIn OAuth2 API** and connect your account (or supply a LinkedIn app's client id/secret with the `w_member_social` scope). |

## Step 4 — Replace environment-specific values

These values were exported from the author's instance and must be changed:

- **Discord server/channel** — guild id `975057983782416464` and channel id
  `1434621031619891220` appear in **5 Discord nodes in `main.json`** ("Send
  message and wait for response", "...response1", "...response2", "Send a
  message", "Send a message1") and **1 Discord node in
  `error-handler.json`**. Re-select your own server and channel in each
  node — after connecting the credential, the server is picked by id and the
  channel from the dropdown list.
- **Audit log path** — `post-creation-flow.json` reads and writes the Excel
  audit log at `/n8n/data/FirstAIMoversP3.xlsx` in two nodes ("Read/Write
  Files from Disk" and "Read/Write Files from Disk1"). The read tolerates a
  missing file (`continueErrorOutput`), but the **write will error** if the
  directory does not exist — point both nodes at a writable path, e.g.
  `/home/node/.n8n/FirstAIMoversP3.xlsx` (inside the compose named volume).
- **Bluesky handle** — `bluesky-branch.json` hardcodes `"repo":
  "mhaytham.bsky.social"` in both `createRecord` HTTP nodes ("Post Creation
  w/ Clickable Link, Hashtag, Embedded Image" and "...Image1"). Replace with
  your handle.
- **LinkedIn person** — the `person` field (`YkzIh5y9Da`) on both LinkedIn
  nodes is the author's member URN; after connecting your own LinkedIn
  credential, re-select your profile in the node.
- **RSS feed URL** — the RSS Feed Trigger in `main.json` polls
  `https://rss.beehiiv.com/feeds/i8rpEDqA4c.xml` every minute. Swap in your
  feed and adjust the poll interval if needed.

## Step 5 — Activate

1. Activate `error-handler.json`'s workflow **first** — an error workflow
  only runs when it is active.
2. Optionally activate the three branch workflows and `post-creation-flow`
   (not required — `executeWorkflow` can call inactive workflows — but
   harmless).
3. Activate `main.json`'s workflow **last**: its RSS Feed Trigger starts
   polling immediately on activation, and you want all sub-workflows and
   credentials ready first.

Toggle workflows in the UI, or via CLI, e.g.
`docker compose exec n8n n8n update:workflow --id=E5LNKyY3Odkk92b1 --active=true`.

## Remote approvals (approving from your phone)

`sendAndWait` approval links use `WEBHOOK_URL`. As long as it points at
`localhost`, the Approve/Decline links only work on this machine. To approve
from another device, expose n8n (e.g. `ngrok http 5678`) and set
`WEBHOOK_URL` to the public URL — full walkthrough, including the Discord
OAuth2 redirect change, in [ngrok-approval-setup.md](ngrok-approval-setup.md).

## Approval behaviour worth knowing

- Each `sendAndWait` approval waits **24 h** (`limitWaitTime`). On timeout
  the approval is re-sent, up to **3 unanswered asks** per platform item —
  after that the platform is skipped for that item and a Discord
  notification is posted. Declining still regenerates the post instead.
- HTTP read/upload nodes retry 3× with 1 s waits; publish calls are
  deliberately **not** retried (avoids duplicate posts).
- Any failing execution triggers `error-handler.json` via
  `settings.errorWorkflow` → Discord notification.
- If you re-export workflows from your instance, run
  `scripts/normalize-exports.py` before committing — it strips `active`,
  `meta` and `versionId` so the checked-in exports stay clean.
