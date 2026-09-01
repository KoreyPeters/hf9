FROM python:3.14-slim

ARG LITESTREAM_VERSION=0.3.13
ADD https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz /tmp/
RUN tar -C /usr/local/bin -xzf /tmp/litestream-*.tar.gz && rm /tmp/litestream-*.tar.gz

WORKDIR /app
COPY . .

RUN pip install uv && uv sync --no-dev
ENV PATH="/app/.venv/bin:$PATH"

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
