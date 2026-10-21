FROM python:3.14-slim

ARG LITESTREAM_VERSION=0.3.13
ADD https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz /tmp/
RUN tar -C /usr/local/bin -xzf /tmp/litestream-*.tar.gz && rm /tmp/litestream-*.tar.gz

WORKDIR /app
COPY . .

# Compile every installed module to bytecode at build time.
#
# `uv sync` does not do this by default, so the image shipped `.py` files with no
# `.pyc` beside them. The consequence is paid on every single container start:
# the first Python process compiles everything it imports — 1,114 modules for
# `django.setup()` alone — and writes `.pyc` files that die with the container.
#
# Measured locally, forcing a cold cache with `PYTHONPYCACHEPREFIX` pointed at an
# empty directory:
#
#   warm bytecode cache   0.99s    django.setup() + MigrationLoader
#   cold bytecode cache   8.38s    the same work, 8.4x
#
# The cold figure matches the 7.6s that `manage.py migrate` took in production,
# which is what made a no-op migration look slow: migrations themselves are
# 0.15s of that. `migrate` was simply the first command to run, so it paid for
# everything, and the two Python starts after it were fast because they read what
# it had just written.
#
# Must stay above the `uv sync` below — it is read during the sync, and moving it
# after would leave it with nothing to act on while still looking correct.
# See plans/cold-start-optimisation.md.
ENV UV_COMPILE_BYTECODE=1

RUN pip install uv && uv sync --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# UV_COMPILE_BYTECODE covers .venv only. This project has no [build-system], so
# uv never installs /app itself, and the app's own modules — every migration
# among them — would still compile from source on each boot. Two of the three
# fault-handler dumps in plans/migrate-slow-tail.md were inside the migration
# loader, compiling or reading exactly those files.
#
# compileall skips anything already current, so passing over .venv again is
# free. The .pyc stays valid at runtime because it records the source's mtime
# and size, and neither changes inside the image.
RUN python -m compileall -q -j 0 /app

# Both of these exist to make a hung startup readable, and the first one is not
# optional comfort — without it the logs actively mislead.
#
# Cloud Run captures stdout through a pipe, so Python block-buffers it. That
# means `manage.py migrate` does not print "Operations to perform:" when it
# reaches that line; it prints when the process *exits* and the buffer flushes.
# A boot that completes shows all its output at once, and a boot that hangs
# shows nothing whatsoever — not because nothing happened, but because nothing
# was flushed. Thirteen failed starts on 2026-09-01 were blank in the logs for
# this reason alone. See plans/startup-hang-and-503s.md.
ENV PYTHONUNBUFFERED=1

# Makes Python dump every thread's stack on a fatal signal. Paired with the
# `timeout -s ABRT` around migrate in start.sh, this turns "it hung again" into
# the exact frame it is parked in.
ENV PYTHONFAULTHANDLER=1
RUN chmod +x /app/start.sh /app/migrate.sh

RUN mkdir -p /data
VOLUME /data

CMD ["./start.sh"]
