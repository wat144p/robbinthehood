#!/bin/sh
set -e

# cron runs jobs with a near-empty environment, so the container's env vars
# never reach run.py. Writing them to /app/.env — which run.py already knows
# how to read — is simpler and less fragile than templating the crontab.
: > /app/.env
chmod 600 /app/.env

for name in EBAY_CLIENT_ID EBAY_CLIENT_SECRET BESTBUY_API_KEY \
            REDDIT_CLIENT_ID REDDIT_CLIENT_SECRET \
            DISCORD_WEBHOOK_URL NTFY_TOPIC; do
    value=$(printenv "$name" || true)
    if [ -n "$value" ]; then
        echo "$name=$value" >> /app/.env
    fi
done

echo "robbin-the-hood starting. Timezone: $(date +%Z), now: $(date)"

# One run at startup so you find out immediately whether the config works,
# rather than up to eight hours later.
if [ "${RUN_ON_START:-true}" = "true" ]; then
    echo "--- initial run ---"
    python run.py --once || echo "Initial run failed; cron will retry on schedule."
fi

echo "--- handing over to cron (every 8 hours) ---"
exec cron -f
