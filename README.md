# Outreach System — Complete Setup

Two parts sharing one Neon database:
- **Render** — your private dashboard (add mailboxes, upload leads, manage templates, view stats)
- **GitHub Actions** — the engine that actually sends emails, on a schedule, unattended

Nothing sensitive is ever stored in plaintext, and mailbox passwords are verified
with a real login attempt before being saved.

---

## 1. Create your Neon database

1. Go to [neon.tech](https://neon.tech), create a free project.
2. Copy the connection string from the dashboard. It looks like:
   `postgresql://user:password@ep-xxxx.neon.tech/dbname?sslmode=require`
3. Tables are created automatically the first time either app runs — no manual SQL needed.

## 2. Generate your encryption key

This key encrypts mailbox passwords at rest. **Both Render and GitHub Actions need the exact same value.**

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Save this output somewhere safe — you'll paste it into both places in the next steps.

## 3. Push this project to a GitHub repo

```bash
git init
git add .
git commit -m "outreach system"
git remote add origin <your repo URL>
git push -u origin main
```

**Never commit `.env`** — it's git-ignored by default in this project. All real credentials
go into Render's environment variables and GitHub Secrets, set up below.

## 4. Deploy the backend on Render

1. New → Web Service → connect your GitHub repo.
2. **Build command:** `pip install -r requirements.txt`
3. **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. **Instance type:** Free is fine — nothing depends on this staying awake, since the
   GitHub Actions engine talks to Neon directly, not through Render.
5. Add these environment variables in Render's dashboard:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | Your Neon connection string |
   | `ENCRYPTION_KEY` | The key from step 2 |
   | `DASHBOARD_USERNAME` | Pick a username (e.g. `admin`) |
   | `DASHBOARD_PASSWORD` | Pick a strong password |

6. Deploy. Open the Render URL — you'll be prompted for the username/password you set above
   (HTTP Basic Auth, browser handles it natively).

## 5. Configure GitHub Actions

In your repo: **Settings → Secrets and variables → Actions**

Add these **Secrets**:

| Secret | Value |
|---|---|
| `DATABASE_URL` | Same Neon connection string as Render |
| `ENCRYPTION_KEY` | The **exact same** key as Render — this is what lets the engine decrypt mailbox passwords Render encrypted |

Add this **Variable** (Variables tab, not Secrets):

| Variable | Value |
|---|---|
| `DAILY_SEND_CAP` | `80` (start lower — see safety notes below) |

## 6. Set your schedule to your target market's timezone

Open `.github/workflows/outreach.yml`. Cron times are in **UTC**. Convert your target
country's business hours to UTC and adjust the `cron:` lines. The default runs the main
daily cycle once, plus 3 extra reply-check passes spread through the day (roughly every 3
hours) so replies don't sit unnoticed for long.

## 7. Add your mailboxes and templates through the dashboard

Open your Render URL:

1. **Mailboxes tab** — add each of your 5 Spacemail accounts. Each one gets a real IMAP
   login attempt immediately — if it fails, nothing is saved and you'll see the exact error.
2. **Templates tab** — add 2-3 `first_touch` templates (your different pitch angles) and
   your `followup` sequence (set `step_order` 1, 2, 3... and `gap_days` for each — e.g.
   followup_1 at 3 days, followup_2 at 4 days after that, etc). Follow-ups don't need a
   subject — they automatically thread as "Re: [original subject]".
3. **Upload Leads** — CSV with `business_name, contact_email` required; optional
   `first_touch_template` column (template name — omit to auto-rotate evenly across your
   active first-touch templates); any other column becomes a `{{field}}` personalization
   placeholder automatically.

## 8. Test before trusting the schedule

Go to your repo's **Actions tab → Outreach Engine → Run workflow** to trigger a run manually.
Add one test lead pointing to your own email first, confirm it actually arrives and threads
correctly, before letting real leads flow through it.

---

## How it all fits together

```
Render dashboard (you, manually) ──► Neon (mailboxes, templates, contacts)
                                            │
GitHub Actions (scheduled, automatic) ──────┤
   - reads mailboxes/templates/contacts     │
   - decrypts passwords, sends via SMTP     │
   - checks replies via IMAP first, always  │
   - writes results back                    ▼
                                       Neon (send_log)
                                            │
Render dashboard (you, anytime) ◄──────────┘
   - reads send_log for live stats
```

## Safety notes

- **Ramp `DAILY_SEND_CAP` gradually** — start at 30-40 for the first few days on a
  fresh/quiet domain, step up toward 80-100+ over 2-3 weeks. This is a domain-wide number,
  shared across all 5 mailboxes — spreading sends across mailboxes helps per-inbox limits,
  but doesn't protect domain reputation, which is shared.
- **Each contact always sends from the same mailbox** for their entire thread (first touch
  + all follow-ups) — this is required for threading to look legitimate; the code already
  handles this automatically via deterministic per-contact assignment.
- **`check` always runs before `followup`/`send`** in the daily cycle — this is what
  guarantees you never accidentally email someone who just replied.
- If a mailbox's password changes or gets locked on Spacemail's end, `check`/`send` will
  print a warning for that mailbox and skip it — it won't crash the whole run.
