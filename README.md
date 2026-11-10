# PS5 Pro stock tracker (PlayStation Direct NL → ntfy)

Watches the PlayStation Direct page
<https://www.playstation.com/nl-nl/ps5/ps5-pro/buy-now/> and sends a push
notification to your phone through the [ntfy](https://ntfy.sh) app when the
PS5 Pro is in stock.

- One Python file (`tracker.py`) with **no dependencies** (Python 3.8+).
- Reads the stock status Sony embeds in the page (schema.org `availability` and
  `stockLevelStatus` for the page's own SKU), so it doesn't depend on button text,
  and other consoles shown on the page can't trigger it.
- Urgent ntfy alert with a tap-to-open "Open shop" button, reminders while it stays in
  stock (after 5, 10, 20, 40 minutes, then hourly), a message when it sells out again,
  and a warning if the tracker itself stops working.

## 1. Set up ntfy (2 minutes)

1. Install the **ntfy** app: [Android (Google Play)](https://play.google.com/store/apps/details?id=io.heckel.ntfy),
   [F-Droid](https://f-droid.org/packages/io.heckel.ntfy/) or [iPhone (App Store)](https://apps.apple.com/app/ntfy/id1625396347).
2. Choose a **secret topic name**. Topics on ntfy.sh are public: anyone who knows the
   name can read or send to it, so don't pick something like `ps5`. Generate one:
   ```bash
   python3 tracker.py --new-topic        # e.g. ps5pro-3f9c2a71b0d84e6a
   ```
3. In the app tap **+**, enter that topic, and subscribe (server: `ntfy.sh`).
   On Android, turning on *Instant delivery* makes alerts arrive faster.
4. Send yourself a test:
   ```bash
   NTFY_TOPIC=ps5pro-3f9c2a71b0d84e6a python3 tracker.py --test-notify
   ```

## 2. Run it

There are two ways to run it. **Option A** (your own always-on device) gives the fastest
alerts. **Option B** (GitHub Actions) needs no device, but checks less often.

### Option A — your own computer, Raspberry Pi or NAS

The fastest option: it checks every ~60 seconds, around the clock, as long as the device is on.

```bash
git clone https://github.com/rsnIndomi/Ps5-tracker.git && cd Ps5-tracker   # public repo; otherwise sign in or download the ZIP
export NTFY_TOPIC=ps5pro-3f9c2a71b0d84e6a
python3 tracker.py
```

Windows (PowerShell):

```powershell
$env:NTFY_TOPIC = "ps5pro-3f9c2a71b0d84e6a"
python tracker.py
```

When it starts you get a low-priority "tracker started" push with the current
status, so you know it's working. Stop it with Ctrl+C.

**Run it 24/7 on Linux / Raspberry Pi** with the included systemd unit:
see the instructions at the top of [`ps5-tracker.service`](ps5-tracker.service).

**Docker** (e.g. Synology / Unraid):

```bash
docker build -t ps5-tracker .
docker run -d --name ps5-tracker --restart unless-stopped \
  -e NTFY_TOPIC=ps5pro-3f9c2a71b0d84e6a -v ps5-tracker-data:/data ps5-tracker
docker logs -f ps5-tracker
```

### Option B — GitHub Actions (no device needed, slower)

[`.github/workflows/ps5-tracker.yml`](.github/workflows/ps5-tracker.yml) runs one
check every 5 minutes on GitHub's servers.

1. **Make the repo public:** *Settings → General →* (bottom) *Danger Zone → Change
   visibility → Public*. The repo contains no secrets; your topic lives in an Actions
   secret, and GitHub masks secrets in the public run logs.
2. *Settings → Secrets and variables → Actions*:
   - *Secrets* tab → *New repository secret*: `NTFY_TOPIC` = your topic
     (and `NTFY_TOKEN` if your topic is protected).
   - *Variables* tab → *New repository variable*: `TRACKER_ENABLED` = `true`.
     Until this exists, scheduled runs are skipped and cost nothing.
3. *Actions* tab → *PS5 Pro stock check* → *Run workflow*, choose **test-notify**:
   your phone should get a test push. (Choose **check-only** to see what GitHub's server
   sees on the page; that option doesn't need ntfy.)

4. **Add an external timer (needed).** GitHub's own schedule asks for every 5 minutes,
   but in practice this repo was started only every **4–7 hours**. Manual starts
   (`workflow_dispatch`) run within seconds, so a free timer service starts the workflow
   every 5 minutes instead:

   1. **Create a GitHub token** that can only start workflows in this repo:
      GitHub → *Settings → Developer settings → Personal access tokens → Fine-grained tokens →
      Generate new token*.
      - *Token name*: `ps5-tracker timer`
      - *Expiration*: the longest offered (put a reminder in your calendar to renew it)
      - *Repository access*: *Only select repositories* → `Ps5-tracker`
      - *Permissions → Repository permissions → Actions*: **Read and write**. Leave everything else at *No access*.
      - Copy the token (starts with `github_pat_`). GitHub shows it only once.
   2. **Create a free account at [cron-job.org](https://cron-job.org)** → *Create cronjob*:
      - *URL*: `https://api.github.com/repos/rsnIndomi/Ps5-tracker/actions/workflows/ps5-tracker.yml/dispatches`
      - *Execution schedule*: every **5** minutes
      - *Advanced* tab → *Request method*: `POST`
      - *Headers* (add four):
        `Authorization` = `Bearer github_pat_…` (your token) ·
        `Accept` = `application/vnd.github+json` ·
        `X-GitHub-Api-Version` = `2022-11-28` ·
        `Content-Type` = `application/json`
      - *Request body*: `{"ref":"main"}`
      - Turn on *Notify me when execution fails*, then save.
   3. Click *Test run* in cron-job.org. A good answer is **HTTP 204** (empty), and a new
      run appears in the *Actions* tab within seconds. `401`/`403` means the token or its
      permission is wrong; `404` means a typo in the URL.

   The token can only manage workflow runs in this one repo (start, cancel, re-run, delete
   runs and logs). It can't read or change your code or secrets, so the risk if cron-job.org
   leaked it is small. You can revoke it on GitHub at any time.

### How do I know it's working?

- **Every morning after 09:00 you get a low-priority push "still watching"**, e.g.
  *"287 checks in the last 24 h (0 failed), about every 5 min. Last good check Tue 09:01:
  out of stock, €899,99."*
  - About **280–290 checks**: everything works.
  - **Only a handful, "too slow" warning**: the external timer isn't running (check cron-job.org).
  - **No morning push at all**: nothing is running. Look at the *Actions* tab.
- If checks start **failing** (blocked, page changed), you get "checks are failing"
  after 3 failed runs in a row, without waiting for the morning.
- cron-job.org emails you if it can't start the workflow (e.g. the token expired).
- Any time: the *Actions* tab lists every run; a green tick means the page was checked.

Change the time with the repository variable `HEARTBEAT_HOUR` (0–23), or set it to `off`.

Know the limits:

- **Keep the repo public.** In a private repo every run is billed as at least 1 minute.
  A 5-minute schedule is about 8,900 runs a month, against the 2,000 minutes included in
  GitHub Free for private repos. Once those run out, GitHub *blocks* all runs until the
  next billing cycle, and you get no warning push, because the tracker never starts.
- **Schedules are best-effort.** Runs are often several minutes late and are sometimes
  skipped when GitHub is busy. Option A is faster.
- **Blocking:** playstation.com runs behind Akamai, which may treat GitHub's data-centre
  IPs differently from a home connection. If checks keep failing you get a "checks are
  failing" push after 3 failed runs.
- GitHub disables scheduled workflows in public repos after 60 days without activity.
  It emails you first; re-enable the workflow in the *Actions* tab or push any commit.
- GitHub's terms describe Actions as being for building, testing and deploying software.
  Small scheduled checks like this are common, but GitHub can disable workflows it
  considers misuse.

## Notifications you can get

| Push | Priority | When |
|---|---|---|
| **PS5 Pro IN STOCK!** | urgent (5) | in stock / pre-order; repeated while it lasts (after `REMIND_EVERY`, then doubling, max hourly) |
| **queue detected – possible drop!** | urgent (5) | the page redirects to a waiting room (Queue-it) |
| stock status changed | high (4) | Sony uses a stock value the tracker doesn't recognise (have a look) |
| checks are failing | high (4) | `ERROR_ALERT_AFTER` checks in a row failed (blocked, offline, page redesigned) |
| still watching | low (2); default (3) if checks were too far apart or some failed | once a day after `HEARTBEAT_HOUR` (default 09:00): number of checks in the last 24 h |
| sold out again / working again / tracker started | low (2) | for context |

## Configuration

Every setting can be passed as a flag or an environment variable.

| Env var | Flag | Default | Meaning |
|---|---|---|---|
| `NTFY_TOPIC` | `--topic` | – (required) | your topic (a full URL like `https://ntfy.example.com/topic` also works) |
| `NTFY_SERVER` | `--server` | `https://ntfy.sh` | self-hosted ntfy server |
| `NTFY_TOKEN` | `--token` | – | access token for protected topics |
| `NTFY_USER` / `NTFY_PASSWORD` | `--user` / `--password` | – | basic auth instead of a token |
| `PRODUCT_URL` | `--url` | the PS5 Pro NL page | any playstation.com `buy-now` product page |
| `TARGET_SKU` | `--sku` | the page's own SKU | watch specific SKU id(s), comma-separated |
| `CHECK_INTERVAL` | `--interval` | `60` | seconds between checks in loop mode (minimum 20) |
| `REMIND_EVERY` | `--remind-every` | `300` | seconds until the first reminder while in stock; then doubles, max hourly |
| `ERROR_ALERT_AFTER` | `--error-alert-after` | `10` | failed checks in a row before a warning push |
| `STATE_FILE` | `--state-file` | loop: none · `--once`: `state.json` next to `tracker.py` | remember state across restarts / runs |
| `NO_START_MESSAGE` | `--no-start-message` | off | skip the "tracker started" push |
| `HEARTBEAT_HOUR` | `--heartbeat-hour` | `9` | local hour for the daily "still watching" push, or `off` |
| `TZ` | – | system | time zone for the time shown in pushes (Docker image and workflow use `Europe/Amsterdam`) |

Other modes: `--check` (one check, prints what it found, never notifies),
`--once` (one check with notifications, for cron; remembers state in `state.json`
so reminders and error warnings work), `--dry-run` (log instead of sending).

```text
$ python3 tracker.py --check
status : OUT_OF_STOCK
product: PlayStation®5 Pro Console - 2 TB
price  : €899,99
skus   : PSD001-PSDE01942_39-PS5PROCHASASIRSE-E000K3A7V
signal : json-ld PSD001-PSDE01942_39-PS5PROCHASASIRSE-E000K3A7V: OutOfStock -> unavailable
signal : sku PSD001-PSDE01942_39-PS5PROCHASASIRSE-E000K3A7V: OUT_OF_STOCK (inventoryOverride=forceOutOfStock) -> unavailable
```

## Limitations

- **Sony's page lags slightly.** The page is cached on Sony's side and was observed
  to be 0–3 minutes old. Add your check interval on top of that. Popular drops can
  sell out in minutes, so be ready to act when the push arrives (signed in to your
  PlayStation account, payment details saved).
- **PlayStation Direct only.** It doesn't watch retailers (Coolblue, bol, MediaMarkt, …).
- The page currently reports `inventoryOverride=forceOutOfStock`, which looks like
  Sony has manually switched the product off rather than simply run out. The tracker
  will notice when that changes, but there's no telling when that will be.
- If Sony redesigns the page, detection may stop working. You'll get a
  "checks are failing" push instead of silence. The exception: if GitHub itself stops
  running the workflow (see Option B), nothing runs to warn you.
- ntfy.sh allows 250 messages a day per IP for free use; the reminder back-off keeps the
  tracker far below that.
- It's polite by design: one page request per check (default every 60 s, well above
  the site's `Crawl-delay: 5`), with random jitter and automatic back-off on errors.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests use small synthetic pages that mirror the real page structure. The
detection logic was also checked against live playstation.com pages: in-stock
products (DualSense Edge, PlayStation Portal), a pre-order product (FlexStrike),
and out-of-stock products (PS5 Pro, PS VR2). The PS VR2 case matters because its
page shows an `ADD_TO_CART` button while out of stock, which is why button state is ignored.
