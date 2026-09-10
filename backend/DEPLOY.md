# Deploying the Cirqle backend to AWS (EC2, Amazon Linux 2023)

Follow this once your AWS account is fully activated. Total hands-on time: ~10 minutes.

The heavy lifting is automated by [`deploy/setup.sh`](deploy/setup.sh) — you launch a
server, connect to it, and paste **one command**.

---

## Stage 1 — Launch the server (AWS console)

1. Sign in to AWS → search **EC2** → open it.
2. **Top-right, set your Region** to **Europe (London) `eu-west-2`** (UK users). The
   server lives in the selected region, so choose it *before* launching.
3. Click **Launch instance**.
4. **Name:** `cirqle-backend`
5. **OS Image (AMI):** **Amazon Linux 2023**, 64-bit (x86). ("Free tier eligible.")
6. **Instance type:** the one labelled **Free tier eligible** (`t2.micro` or `t3.micro`).
7. **Key pair:** Create new key pair → `cirqle-key`, RSA, `.pem` → save the download
   to `~/.ssh/` (backup; we connect via the browser).
8. **Network settings → Edit → add these firewall rules:**

   | Type       | Port | Source          |
   |------------|------|-----------------|
   | SSH        | 22   | Anywhere 0.0.0.0/0 |
   | HTTP       | 80   | Anywhere        |
   | HTTPS      | 443  | Anywhere        |
   | Custom TCP | 8000 | Anywhere        |

9. **Storage:** default 8 GB is fine.
10. **Launch instance** → **View all instances** → wait for **Running** + **2/2 checks passed**.
11. Copy the **Public IPv4 address**.

## Stage 2 — Connect to the server (from your Mac's Terminal)

SSH is locked to your IP, so connect with the key you downloaded. (The browser
"Connect" button won't work with a My-IP rule — it comes from AWS's IP, not yours.)

```bash
mv ~/Downloads/cirqle-key.pem ~/.ssh/ 2>/dev/null   # if it's still in Downloads
chmod 400 ~/.ssh/cirqle-key.pem                      # lock down the key file
ssh -i ~/.ssh/cirqle-key.pem ec2-user@YOUR_SERVER_IP
```

Type `yes` at the fingerprint prompt the first time. You're now on the server.

## Stage 3 — Bring the API online (one command)

Paste this into that browser terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/gkg-gkg/cirqle-proj/main/backend/deploy/setup.sh | bash
```

It installs Python, pulls the code, creates a virtualenv, generates a secret key,
and starts the API as a background service. When it finishes you'll see the service
marked **active (running)**.

## Stage 4 — Test it live

From your **laptop** (replace with your instance's public IP):

```bash
curl http://YOUR_SERVER_IP:8000/
# {"status":"ok","service":"cirqle-api"}
```

## Stage 5 — Point the website at the live API

Edit [`assets/api.js`](../assets/api.js) so the production URL is your server, then
commit + redeploy the frontend. Login/signup on the live site now use AWS.

> ⚠️ **Mixed content:** if your website is served over **https://**, the browser will
> block calls to a plain **http://** API. So for the *public* site to work, the API
> needs HTTPS — see Stage 6. (Local testing over http:// works right away.)

## Stage 6 — HTTPS + domain (production)

1. Point a domain/subdomain (e.g. `api.cirqle.example`) at the server's public IP
   (an **A record**).
2. Install [Caddy](https://caddyserver.com), which auto-provisions a free TLS
   certificate. Reverse-proxy `443 → 127.0.0.1:8000`. (Commands provided when you
   reach this step.)
3. Update `assets/api.js` to the `https://api.cirqle.example` URL.

---

## Managing the service (handy commands, run on the server)

```bash
sudo systemctl status cirqle-api     # is it running?
sudo systemctl restart cirqle-api    # restart it
journalctl -u cirqle-api -n 50 --no-pager   # last 50 log lines
```

## Enabling the Instagram feed (Apify token)

The feed's Refresh button scrapes Instagram **server-side**, so the Apify token
lives only in the backend `.env` — never in the browser. On the server:

```bash
nano ~/cirqle/backend/.env         # add:  APIFY_TOKEN=apify_api_...
sudo systemctl restart cirqle-api  # pick up the new value
```

Fresh installs already have a blank `APIFY_TOKEN=` line ready to fill in. Until a
token is set, `POST /feed/refresh` returns 503 with a clear message.

## Deploying a code update later

Re-run the same one-liner from Stage 3 — it pulls the latest code and restarts the
service, keeping your `.env` and database intact.

---

## Transactional email (verification links + password resets)

Sign-up now requires confirming an email address, and passwords can be reset by
link. Both need Amazon SES. **Until SES is set up and out of sandbox, no member
can complete a sign-up on the live site** — so do these in order.

### 1. One-off AWS setup

1. **Verify the sending domain.** SES console (region **eu-west-2**) →
   Configuration → Identities → Create identity → Domain → `cirqle.co.uk`,
   Easy DKIM, RSA_2048_BIT. Add the 3 CNAME records it gives you to Krystal DNS
   as *relative* names (`abc._domainkey`, not `abc._domainkey.cirqle.co.uk` —
   Krystal appends the domain itself). Status goes Pending → Verified.
2. **Request production access.** SES → Account dashboard → Request production
   access → Transactional. **Takes ~24 hours.** Until it's granted, SES delivers
   only to addresses you've individually verified — everyone else gets nothing,
   silently.
3. **Permission.** The EC2 role `cirqle-ec2-s3` needs an inline policy allowing
   `ses:SendEmail` and `ses:SendRawEmail` on `*`. No restart needed.

### 2. On the server

`setup.sh` only writes `.env` when it doesn't exist, so add these by hand:

```bash
ssh -i ~/.ssh/cirqle-key.pem ec2-user@35.178.6.182
nano ~/cirqle/backend/.env
```

```
CIRQLE_EMAIL_MODE=console
CIRQLE_EMAIL_FROM=Cirqle <noreply@cirqle.co.uk>
CIRQLE_SITE_BASE=https://cirqle.co.uk
```

Leave `CIRQLE_EMAIL_MODE=console` until step 1.2 is granted. In console mode
emails are written to the service log instead of being sent — nothing breaks,
but nobody receives anything, so links must be read out of the log:

```bash
journalctl -u cirqle-api -n 100 --no-pager | grep -A 6 "EMAIL (console mode"
```

Once production access is granted, change it to `ses` and restart:

```bash
sudo systemctl restart cirqle-api
```

### 3. Deploy the code

```bash
curl -fsSL https://raw.githubusercontent.com/gkg-gkg/cirqle-proj/main/backend/deploy/setup.sh | bash
```

This pulls the code and runs `alembic upgrade head`, which applies migration
`d5e1a8c30b47` (the `authtoken` table plus verification columns). Existing
accounts are grandfathered as already-verified, so nobody is locked out.

### 4. Expect everyone to be signed out once

Login tokens now carry a stamp of when the password last changed, so that
changing a password invalidates sessions on other devices. Tokens issued before
this deploy have no stamp and are refused. **Every signed-in user is logged out
once** and signs back in as normal. This happens only on this deploy.

### 5. Check it works

```bash
curl -s https://api.cirqle.co.uk/ping >/dev/null; \
curl -s -X POST https://api.cirqle.co.uk/auth/forgot-password \
  -H 'Content-Type: application/json' -d '{"email":"you@example.com"}'
```

Always answers "if that address has an account…" whether or not it does — that
is deliberate, so the endpoint can't be used to discover who has an account.
Then check the inbox (or the log, in console mode).

### Ordering note

The website is served by GitHub Pages and goes live the moment you push, but the
API only updates when you run `setup.sh`. Push and deploy close together —
between the two, the live site calls endpoints the old API doesn't have.

### Known gap (pre-existing)

The merchant brand-profile columns (`bio`, `categories`, `website`, …) are added
by `scripts/migrate_merchant_profile.py`, not by an Alembic migration, so a
database built purely from migrations will not have them and merchant endpoints
will 500. Existing deployments already ran it. Run it once on any fresh
database:

```bash
cd ~/cirqle/backend && .venv/bin/python scripts/migrate_merchant_profile.py
```
