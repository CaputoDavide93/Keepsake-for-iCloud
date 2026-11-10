FROM alpine:3.23@sha256:ff7f0c727dd796aa36787bfcd9a64bf35a6ce72a73451b3359fde849b6703d63

# Python's standard library does all the work (copy, compare, verify, serve the
# web page): no pip, no build toolchain, no compiled dependencies. tzdata is
# there because a schedule such as 03:30 is wall-clock time in your TZ.
RUN apk add --no-cache python3 tzdata \
    && python3 -c "import json, hashlib, zoneinfo, http.server" \
    && adduser -D -u 10001 keepsake \
    && install -d -o keepsake -g keepsake /data

COPY src/keepsake /opt/keepsake/keepsake

ENV PYTHONPATH=/opt/keepsake \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KEEPSAKE_DATA=/data \
    KEEPSAKE_HOST=0.0.0.0 \
    KEEPSAKE_PORT=8765

# Jobs, state, settings and the activity log live in /data. The root filesystem
# is meant to run read-only (see docker-compose.yml): only /data and /tmp are
# written, and /icloud when you mount it.
VOLUME ["/data"]
EXPOSE 8765

# 0.0.0.0 is the only address a published port can reach, so the image refuses
# to start unless KEEPSAKE_PASSWORD (or KEEPSAKE_PASSWORD_FILE) is set.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('KEEPSAKE_PORT', '8765'), timeout=3)"]

USER keepsake
WORKDIR /opt/keepsake
ENTRYPOINT ["python3", "-m", "keepsake"]
