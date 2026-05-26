FROM python:3.14-slim

ARG LITESTREAM_VERSION=0.3.13
ADD https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz /tmp/
RUN tar -C /usr/local/bin -xzf /tmp/litestream-*.tar.gz && rm /tmp/litestream-*.tar.gz

WORKDIR /app
COPY . .

RUN pip install uv && uv sync --no-dev
RUN python manage.py collectstatic --noinput

RUN mkdir -p /data
VOLUME /data

CMD ["./start.sh"]
