# PS5 Pro stock tracker (PlayStation Direct NL → ntfy)

Watches the PlayStation Direct page
<https://www.playstation.com/nl-nl/ps5/ps5-pro/buy-now/> and sends a push
notification to your phone through the [ntfy](https://ntfy.sh) app when the
PS5 Pro is in stock.

- One Python file (`tracker.py`) with **no dependencies** (Python 3.8+).
- Reads the stock status Sony embeds in the page (schema.org `availability` and
  `stockLevelStatus` for the page's own SKU), so it doesn't depend on button text,
  and other consoles shown on the page can't trigger it.
- Urgent ntfy alert with a tap-to-open "Open shop" button, a reminder every 5 minutes
  while it stays in stock, a message when it sells out again, and a warning if the
  tracker itself stops working.

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

### Option A — your own computer, Raspberry Pi or NAS (recommended)

The fastest option: it checks every ~60 seconds, around the clock, as long as the device is on.

```bash
git clone https://github.com/rsnIndomi/Ps5-tracker.git && cd Ps5-tracker
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

1. Repo **Settings → Secrets and variables → Actions**:
   - *Secrets* tab: add `NTFY_TOPIC` (and `NTFY_TOKEN` if your topic is protected).
   - *Variables* tab: add `TRACKER_ENABLED` = `true`. Until this exists, scheduled runs are skipped and cost nothing.
2. **Actions** tab → *PS5 Pro stock check* → *Run workflow* to test it once.

Before you rely on it, know the limits:

- **Private repo = not free at this frequency.** Every run is billed as at least
  1 minute. A 5-minute schedule is about 8,900 runs a month; the GitHub Free plan
  includes 2,000 minutes for private repos. Make the repo public (standard runners
  are free there), slow the schedule down (e.g. `*/30`), or use Option A.
- **Schedules are best-effort.** Runs are often several minutes late and are
  sometimes skipped when GitHub is busy.
- **It may get blocked.** playstation.com runs behind Akamai, which may treat
  GitHub's data-centre IPs differently from a home connection. This hasn't been
  tested from GitHub's runners. If checks keep failing you get a "checks are failing"
  push after 3 failed runs.
- GitHub may disable scheduled workflows in repos with no activity for 60 days.

## Notifications you can get

| Push | Priority | When |
|---|---|---|
| **PS5 Pro IN STOCK!** | urgent (5) | in stock / pre-order; repeated every `REMIND_EVERY` seconds while it lasts |
| **queue detected – possible drop!** | urgent (5) | the page redirects to a waiting room (Queue-it) |
| stock status changed | high (4) | Sony uses a stock value the tracker doesn't recognise (have a look) |
| checks are failing | high (4) | `ERROR_ALERT_AFTER` checks in a row failed (blocked, offline, page redesigned) |
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
| `REMIND_EVERY` | `--remind-every` | `300` | repeat the in-stock alert this often while in stock |
| `ERROR_ALERT_AFTER` | `--error-alert-after` | `10` | failed checks in a row before a warning push |
| `STATE_FILE` | `--state-file` | – | remember state across restarts / `--once` runs |
| `NO_START_MESSAGE` | `--no-start-message` | off | skip the "tracker started" push |

Other modes: `--check` (one check, prints what it found, never notifies),
`--once` (one check with notifications, for cron), `--dry-run` (log instead of sending).

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
  "checks are failing" push instead of silence.
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
