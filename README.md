# robbin-the-hood

A self-hosted deal-hunting agent for gaming laptops. Monitors US, Canadian, UK,
German, Belgian, Swedish and Australian retailers and marketplaces every 8
hours, normalises every price to **landed USD**, scores what it finds against a
weighted rubric, and pushes only genuinely new, genuinely good results.

Built for buying from Pakistan via forwarding contacts who receive domestic
shipments in any of those seven countries.

---

## The one thing to understand

Every comparison happens in **landed USD**, never in sticker prices:

```
landed_usd = (sticker_local
              − reclaimable_tax          ← always 0; EU/UK VAT is NOT reclaimable
              + domestic_shipping
              + destination_tax_at_checkout)   ← US states and CA provinces only
             × fx_rate_to_usd
             × (1 + regional_risk_premium)     ← US/CA 0%, UK 3%, DE/BE/SE/AU 5%
```

A £1,150 UK listing lands at **$1,504** — over the $1,400 ceiling — while a
$1,150 US listing in a no-sales-tax state lands at exactly **$1,150**. There is
a test asserting precisely that, because getting it wrong is the single most
expensive mistake this system could make.

European sticker prices are VAT-inclusive by law, and because your contact
receives the goods as an ordinary domestic consumer, **that VAT is not
reclaimable**. We never strip it out to make European prices look competitive.

The FX rate and the moment it was fetched are stored on every listing, so a
comparison made weeks later against today's record is still honest.

---

## Quick start

```bash
python -m pip install -r requirements-dev.txt
```

```bash
python -m pytest
```

```bash
python demo.py
```

`demo.py` needs no keys and makes no network calls — it runs the genuine
pipeline against recorded-shape payloads, with only the socket faked.

Then, for a real run that prints instead of sending:

```bash
python run.py --once --dry-run
```

---

## Setup

### 1. Credentials

`cp .env.example .env` and fill in whichever you want. **Every one is
optional** — a source with no credentials reports itself as unconfigured and
the run continues with the others.

| Variable | Where to get it | Cost |
|---|---|---|
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | [developer.ebay.com](https://developer.ebay.com) → **Production** keyset (Sandbox returns fake inventory) | free |
| `BESTBUY_API_KEY` | [developer.bestbuy.com](https://developer.bestbuy.com) → issued instantly | free |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | reddit.com/prefs/apps → "create another app" → type **script**, redirect URI `http://localhost` | free |
| `DISCORD_WEBHOOK_URL` | Channel settings → Integrations → New Webhook → Copy URL | free |
| `NTFY_TOPIC` | Any long **random** string; subscribe to it in the ntfy app | free |

> **ntfy topics are not private.** Anyone who knows the topic name can read it
> *and* publish to it. Use a random string, not `laptop-deals`. Point
> `notification.channels.ntfy.server` at your own instance if you'd prefer.

### 2. Deploy

**GitHub Actions** (recommended — no machine to keep awake):

1. Push this repo to GitHub.
2. Settings → Secrets and variables → Actions → add each secret above.
3. The workflow at `.github/workflows/hunt.yml` runs on `0 */8 * * *`.

The SQLite database is committed back to the repo after each run so dedup
state, price history and floors survive between runs.

> **Two Actions caveats.** GitHub disables scheduled workflows after **60 days
> of repository inactivity** — but because every run commits the database back,
> the repo is never inactive, so this never fires in practice. And Actions cron
> is best-effort: it **drifts by several minutes** under load, occasionally
> much more. Nothing here depends on exact timing.

**Docker** (if you'd rather self-host):

```bash
docker compose up -d
```

Runs once immediately so you find out straight away whether the config works,
then every 8 hours via cron. `docker compose logs -f` to watch.

---

## What you'll actually receive

| Score | What happens |
|---|---|
| **≥ 75**, or a standing priority rule | Immediate alert, one message per listing |
| **55–74** | Batched into the daily digest |
| **< 55** | Logged to the database, silent |

Standing priority rules fire regardless of score:
- **Acer Predator Helios Neo 16S AI** at or under **$1,250 landed**
- Any **confirmed RTX 5070 12 GB**

A listing already notified stays quiet unless its landed price drops more than
5%, which re-alerts as a **PRICE DROP**. Beating a model's record low adds a
**NEW RECORD LOW** banner.

**If nothing clears the bar, nothing is sent.** No "found 0 deals" message.

Every alert carries: model, region + flag, local sticker **and** landed USD side
by side, delta vs. the known floor, the full parsed spec line, keyboard layout,
condition and seller trust, the score with a one-line breakdown of what drove
it, any UNVERIFIED/HIGH RISK flags, a warranty note, and the direct URL. When a
non-US pick wins, it says why — e.g. *"CAD weakness plus AB at 5% tax vs the 13%
baseline puts it $91 under the best US listing."*

One digest per day at **09:00 PKT** with the top 10, price movements, listings
that vanished, sources that failed, and any newly discovered sources awaiting
your approval.

---

## Sources

Verified live on 2026-08-17. **What the brief assumed and what is actually true
have drifted**, so this is the real state of play:

| Source | Status | Notes |
|---|---|---|
| eBay Browse API | ✅ working | 5 regions through one OAuth integration |
| Best Buy API | ✅ working | open-box condition tiers; that endpoint is beta |
| OzBargain (AU) | ✅ working | RSS + `ozb:meta` real destination URL |
| HotUKDeals (GB) | ✅ working | RSS + `pepper:merchant` price/retailer |
| mydealz (DE) | ✅ working | same Pepper platform; feed is `/rss/hot` |
| Reddit | ⚠️ needs OAuth | the `.json` trick is dead — see below |
| RedFlagDeals (CA) | ❌ gated | 307s to a paid bot-access tollbooth |
| Slickdeals (US) | ❌ gated | 403s non-browser clients |
| gaminglaptop.deals | ⚠️ needs work | React SPA; no prices in server HTML |
| bestlaptop.deals | ⚠️ needs work | same, client-rendered |
| Retailer scrapers | ⚠️ disabled | selectors need one `--probe` pass each |
| X / Twitter | ⛔ skipped | API ~$200/mo; use an RSS bridge instead |

### Reddit needs OAuth now

`www.reddit.com/r/X/new.json` returns **403 with any User-Agent**, and
`old.reddit.com` returns 200 but serves an HTML interstitial. Registering a free
script app (above) fixes it. Without credentials the source reports the 403 and
explains the fix, rather than silently returning nothing.

### Finishing the Tier 0 trackers

gaminglaptop.deals is **not** bot-walled — it returns 165 KB to a polite
User-Agent. But it's a React SPA with zero price strings in the server HTML. It
does expose a JSON API (`/api/gpus` works), and references `/api/model-hub`,
which 404s on every parameter shape tried.

**The 20-minute finish:** open the site in a browser with devtools on the
Network tab, filter to XHR, and read off the actual deals request. That makes
it a clean API source, which beats scraping.

**Zero-engineering alternative available today:** gaminglaptop.deals runs a free
price-drop email alert service — pick model, config and target price. Set one up
for the Helios Neo 16S AI at $1,250 and you get their coverage for nothing.

### Enabling a scraper

Every site under `sources.html.sites` ships **disabled with empty selectors**,
because selectors written without seeing a site's live markup are fiction, and a
scraper that silently returns zero looks exactly like "no deals today".

```bash
python run.py --probe currys
```

That fetches one page and prints what the configured selectors actually match.
Fill in `selectors` in `config.yaml` until it looks right, then set
`enabled: true`. Geizhals is the highest-value one — it aggregates most German
retailers in a single query.

### Sites that need a headless browser

Some sites (Newegg, gaminglaptop.deals, bestlaptop.deals — confirmed live)
return HTTP 200 with an empty page shell, because their listings are built by
JavaScript running in the browser after the page loads. A plain
`requests.get()` never sees that content, however polite the User-Agent —
there is no header that fixes it, because nothing is being blocked.

The fix is a **headless browser**: a real browser engine (Chromium — the same
one behind Chrome and Edge) with no visible window, driven by code. It loads
the page, runs the JavaScript, waits for the real content to appear, and hands
back the fully-rendered HTML. It is slower and heavier than a plain request,
which is why it's opt-in per site rather than the default.

```bash
python -m pip install -r requirements-headless.txt
playwright install chromium    # downloads the actual browser, ~150-300 MB, once
```

Mark the site with `render: js` in `config.yaml`:

```yaml
newegg:
  enabled: false
  render: js
  base_url: "https://www.newegg.com"
  ...
```

Then probe it exactly as before — `--probe` uses the headless path
automatically for any site with `render: js` set:

```bash
python run.py --probe newegg
```

If Playwright or its browser binary isn't installed, `--probe` and a live run
both fail with the exact `pip install` / `playwright install` command needed,
rather than a stack trace.

**This will not help every blocked site.** Currys and Geizhals return HTTP 403
— a bot wall, not a rendering problem. A real browser fingerprint sometimes
gets past a simple check, but serious bot protection (Cloudflare, Akamai,
PerimeterX) explicitly detects headless/automation signals and blocks those
too. Worth trying `render: js` on them; not guaranteed to work, and not worth
an arms race for a personal tool if it doesn't.

### Adding a new source

One class implementing `Source.fetch()` in `dealhunter/sources/`, one block
under `sources:` in `config.yaml`, one line in `build_sources()`. Nothing
downstream changes — sources return plain `Listing` objects and know nothing
about scoring or notification.

A source that throws is caught, recorded, and reported in the digest; it never
takes the run down. A source that gets a 403 or a captcha is disabled for the
rest of the run rather than retried into a longer ban.

### Monthly source discovery

On the first run of each month, the agent harvests the sidebars and wikis of
subreddits you already read, checks a seed list, and writes candidates to
`discovered_sources.yaml` with a confidence score. They're surfaced in the next
digest.

**Nothing is ever auto-enabled.** To adopt one, set its `status: approved` and
add a matching entry under `sources.rss.feeds` or `sources.html.sites`. Setting
`status: rejected` stops it being resurfaced.

---

## Tuning

Everything lives in `config.yaml` — budget, hard filters, scoring weights,
enabled regions, tax rates, risk premiums, target models, price floors, and
per-source settings. You should never have to edit Python to retune.

The seven base scoring components sum to exactly 100, and that's **checked at
startup** — if you edit a weight without rebalancing, the run fails loudly
instead of quietly producing scores that can't reach the alert threshold.

```
VRAM tier            30    what model size fits on the card at all
Memory bandwidth     10    how fast tokens come out of it
Panel                15
System RAM           15
Storage               8
GPU power (TGP)      10    wattage beats the model name
Condition / trust    12
```

Modifiers on top: price vs. floor (±10), free M.2 slot (+2), single-channel RAM
(−3), junk title (−15), ISO/bilingual keyboard (−4).

### Price floors

Seeded from `known_models` in config, then **lowered automatically** when a
verified cheaper listing appears. "Verified" is deliberately strict — the floor
feeds the ±10 scoring component, so a bad one silently poisons every future
score for that model. A listing only qualifies if it passed every hard filter,
matched a known model, came from a structured source (not a community claim),
and isn't flagged HIGH RISK, multi-variation, or priced with a stale FX rate.

---

## CLI

```bash
python run.py --once --dry-run          # one pass, print instead of sending
python run.py --once --source ebay      # one source only (repeatable)
python run.py --once --offline          # cached/fallback FX, no FX calls
python run.py --once --force-digest     # send the digest regardless of time
python run.py --once --ignore-state     # treat everything as new
python run.py --once --show-rejected    # what got filtered out, and why
python run.py --probe currys            # what a site's selectors actually match
python run.py --once --discover         # run source discovery now
python run.py --stats                   # database statistics
python run.py --prune                   # drop old history, compact the DB
```

---

## Layout

```
config.yaml            every tunable, and the only file you should need to edit
run.py                 CLI entry point
demo.py                the real pipeline against recorded payloads, no network
dealhunter/
  models.py            Listing / ParsedSpecs / LandedCost / ScoreResult
  config.py            config loading + startup validation
  hardware.py          laptop GPU VRAM + bandwidth tables (hardcoded, not inferred)
  parsing.py           spec extraction and all eight spec-trap validators
  pricing.py           price extraction from human-written deal titles
  regions.py           landed-cost maths and keyboard-layout resolution
  geo.py               postal code → tax jurisdiction
  fx.py                live FX, 12h cache, offline fallbacks
  filters.py           the hard filters
  scoring.py           the 0–100 rubric
  evaluate.py          the pipeline that wires the above together
  store.py             SQLite: dedup, price history, floors, run bookkeeping
  robots.py            robots.txt compliance (RFC 9309)
  discovery.py         monthly source discovery, approval-gated
  sources/             ebay, bestbuy, rss, reddit, html + the Source interface
  notify/              render, router, discord, ntfy, console
tests/                 435 tests, no network required
.github/workflows/     the 8-hourly schedule
docker-compose.yml     self-hosted alternative
```

---

## Known gaps

- **Sweden and Belgium** have no dedicated retailer sources. 25% VAT plus Nordic
  layout, and AZERTY respectively, make them rarely competitive — as the brief
  allowed.
- **Retailer scrapers ship disabled.** See "Enabling a scraper" above.
- **Multi-variation eBay listings are flagged, not resolved.** Their advertised
  price is the cheapest variant, which may not be the config in the title.
  Resolving properly needs a second API call per listing to
  `item_summary/search?item_group_id=`.
- **Bandwidth table**: the 5060/5070/5070 Ti/3070/3080 Ti figures come from the
  spec and are authoritative; the rest are published reference figures. They
  feed a 10-point linear score, so they won't distort much.
