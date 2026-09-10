# Outreach System — Complete Setup

Two parts sharing one Neon database:
- **Render** — your private dashboard (manage campaigns, mailboxes, templates, and view stats)
- **GitHub Actions** — the engine that actually sends emails, on a schedule, unattended

Nothing sensitive is ever stored in plaintext, and mailbox passwords are verified with a real login attempt before being saved.

---

## 1. Create your Neon database

1. Go to [neon.tech](https://neon.tech), create a free project.
2. Copy the connection string from the dashboard. It looks like:
   `postgresql://user:password@ep-xxxx.neon.tech/dbname?sslmode=require`
3. Tables are created automatically the first time the app runs — no manual SQL needed.

## 2. Generate your encryption key

This key encrypts mailbox passwords at rest. **Both Render and GitHub Actions need the exact same value.**

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Save this output somewhere safe — you'll paste it into both places in the next steps.

## 3. (Optional) Google Sheets Sync Setup

If you want to pull leads directly from Google Sheets and automatically push their reply/trial status back:

1. Go to the [Google Cloud Console](https://console.cloud.google.com).
2. Create a new project, enable the **Google Sheets API** and **Google Drive API**.
3. Create a **Service Account** under IAM & Admin.
4. Generate a new JSON key for this service account and download it.
5. In your Google Spreadsheet, click **Share** and invite the `client_email` found in that JSON file with "Editor" access.
6. Open the downloaded JSON file — you'll need to paste its exact contents into the `GOOGLE_SERVICE_ACCOUNT_JSON` environment variable in the next steps.

## 4. Deploy the backend on Render

1. New → Web Service → connect your GitHub repo.
2. **Build command:** `pip install -r requirements.txt`
3. **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Add these environment variables in Render's dashboard:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | Your Neon connection string |
   | `ENCRYPTION_KEY` | The key from step 2 |
   | `DASHBOARD_USERNAME` | Pick a username (e.g. `admin`) |
   | `DASHBOARD_PASSWORD` | Pick a strong password |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | Optional: Paste the full raw JSON content from step 3 |

5. Deploy. Open the Render URL — you'll be prompted for the username/password you set above.

## 5. Configure GitHub Actions

In your GitHub repo: **Settings → Secrets and variables → Actions**

Add these **Secrets**:

| Secret | Value |
|---|---|
| `DATABASE_URL` | Same Neon connection string as Render |
| `ENCRYPTION_KEY` | The **exact same** key as Render |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Optional: Same JSON as Render |

Add this **Variable** (Variables tab, not Secrets):

| Variable | Value |
|---|---|
| `DAILY_SEND_CAP` | `80` (start lower — see safety notes below) |

## 6. How to use Campaigns, Mailboxes, and Templates

The dashboard is designed around **Campaigns**. A campaign maps to a specific Google Sheet and completely isolates its own Mailboxes and Templates.

### Step 1: Create a Campaign
Open your dashboard and click the **"+ New"** button in the top navigation bar. 
- Give it a name.
- If using Google Sheets sync, paste the **Google Sheet ID** (the long string of letters and numbers in your spreadsheet URL).
- Enter the **Google Sheet Tab** (e.g., `Sheet1` or `Q3 Leads`).

### Step 2: Add Mailboxes
Select your campaign from the top dropdown, then go to the Mailboxes section.
- Choose your provider (**Spacemail** or **Gmail**).
- Enter the email and App Password.
- These mailboxes will *only* be used for the currently selected campaign. 

### Step 3: Add Templates
With your campaign selected, go to the Templates section.
- Add 2-3 `first_touch` templates (your different pitch angles).
- Add your `followup` sequence (set `step_order` 1, 2, 3... and `gap_days` for each). Follow-ups don't need a subject — they automatically thread as "Re: [original subject]".
- Like mailboxes, templates belong exclusively to the active campaign.

### Step 4: Upload Leads
- **CSV Upload:** Must have `business_name, contact_email` columns. Any extra columns automatically become `{{field}}` personalization placeholders.
- **Google Sheets Sync:** Click "Pull New Leads from Sheet" to instantly sync new rows from the exact Google Sheet ID and Tab you configured for this campaign. 

## 7. Set your schedule to your target market's timezone

Open `.github/workflows/outreach.yml`. Cron times are in **UTC**. Convert your target
country's business hours to UTC and adjust the `cron:` lines. The default runs the main
daily cycle once, plus 3 extra reply-check passes spread through the day (roughly every 3
hours) so replies don't sit unnoticed for long.

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
   - loops through active campaigns         │
   - syncs leads from specific sheet tabs   │
   - decrypts passwords, sends via SMTP     │
   - checks replies via IMAP first, always  │
   - pushes statuses back to sheet tabs     ▼
                                       Neon (send_log)
                                            │
Render dashboard (you, anytime) ◄───────────┘
   - reads send_log for live stats per campaign
```

## Safety notes

- **Ramp `DAILY_SEND_CAP` gradually** — start at 30-40 for the first few days on a fresh domain, step up toward 80-100+ over 2-3 weeks.
- **Each contact always sends from the same mailbox** for their entire thread (first touch + all follow-ups) — this is automatically handled.
- **`check` always runs before `followup`/`send`** in the daily cycle — this guarantees you never accidentally follow up with someone who just replied.
- If a mailbox's password changes or gets locked, the engine will safely skip it and print a warning rather than crashing.
