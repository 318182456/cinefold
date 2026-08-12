# ---------------------------------------------------------------- 前端
# dist 是生成产物，不进仓库，在这里构建。
FROM node:20-alpine AS web

WORKDIR /web
# 先只拷依赖清单，依赖没变时这层能复用缓存
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build


# ---------------------------------------------------------------- 运行
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/ \
    DOCKER_ENV=true \
    TZ=Asia/Shanghai \
    ENVIRONMENT=production \
    DATA_DIR=/data \
    CONFIG_DIR=/data/config \
    LOG_DIR=/data/logs

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nginx supervisor ca-certificates tzdata curl \
    && ln -sf /usr/share/zoneinfo/$TZ /etc/localtime \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app
COPY main.py /app/main.py
COPY VERSION /app/VERSION

COPY --from=web /web/dist /usr/share/nginx/html

COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY deploy/starting.html /etc/nginx/html/__starting.html
COPY deploy/supervisord.conf /etc/supervisord.conf

VOLUME ["/data"]
EXPOSE 3750

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:3750/api/health || exit 1

CMD ["supervisord", "-c", "/etc/supervisord.conf"]
