# Self-hosting alternative to GitHub Actions.
#
# Worth using if you'd rather not depend on Actions cron drift, or on GitHub
# not disabling the schedule after 60 days of repo inactivity.

FROM python:3.12-slim

# cron for scheduling; tzdata so the container agrees with you about what
# 09:00 PKT means.
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Karachi
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so a code edit doesn't invalidate the layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dealhunter/ ./dealhunter/
COPY run.py config.yaml ./

# The database lives here; mount it as a volume so state survives a rebuild.
RUN mkdir -p /app/data

COPY docker/crontab /etc/cron.d/robbin
RUN chmod 0644 /etc/cron.d/robbin && crontab /etc/cron.d/robbin

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
