# ---------------------------------------------------------------- 前端
# dist 是生成产物，不进仓库，在这里构建。
FROM node:20-alpine AS web

WORKDIR /web
# 先只拷依赖清单，依赖没变时这层能复用缓存
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build


# ---------------------------------------------------------------- 依赖
# 依赖单独装一层，清理干净后把整个 site-packages 拷进运行镜像。
# 关键在于清理必须和 pip install 处在同一个 RUN 里、且发生在拷贝之前：
# Docker 的层只增不减，先装后删只会让被删的内容留在下层继续占体积。
FROM python:3.12-slim-bookworm AS deps

# binutils 提供 strip（下面要用），gcc 兜底应对某个依赖临时没有 wheel 的情况。
# 都只存在于这一层，不会进最终镜像
RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils gcc \
    && rm -rf /var/lib/apt/lists/*

# 清理的内容：依赖自带的测试套件、Cython 源码与 C 头文件都不参与运行；
# .so 里的调试符号占很大一块，lxml / pydantic_core / psycopg 尤其明显。
# 被删掉的 testing 子包里 sqlalchemy/testing 是可导入的（SQLAlchemy 给下游
# 用的测试夹具），但本项目不引用它，删了不影响 sqlalchemy 本体。
#
# 注意别把 pip 删了：热更新 (services/upgrade.py) 在新版引入新依赖时会跑
# `python -m pip install -r requirements.txt`，没有 pip 那条路就断了。
#
# strip 后面不加 `|| true`：真失败了说明这层的假设不成立（比如 binutils
# 没装上），应当让构建当场报错，而不是悄悄少省一截体积。
# 注释一律写在 RUN 外面 —— 夹在 `&&` 续行中间的 `#` 会被 shell 当成注释，
# 把后面那条命令一起吞掉，直接变成语法错误。
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && SP=$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])') \
    && find "$SP" -type d \( -name tests -o -name test -o -name testing \) -prune -exec rm -rf {} + \
    && find "$SP" -type d -name '__pycache__' -prune -exec rm -rf {} + \
    && find "$SP" -type f \( -name '*.pyx' -o -name '*.pxd' -o -name '*.c' -o -name '*.h' \) -delete \
    && find "$SP" -name '*.so' -print0 | xargs -0 --no-run-if-empty strip --strip-unneeded


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

# 别换成 nginx-light：bookworm 起 nginx-light / -core / -full 都退化成
# 空的 metapackage，装的是同一个 nginx 二进制，省不了体积，还在 trixie 里
# 被移除了。curl 只给 HEALTHCHECK 用，换成 python 探测会拖慢健康检查
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nginx supervisor ca-certificates tzdata curl \
    && ln -sf /usr/share/zoneinfo/$TZ /etc/localtime \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# 装好并瘦身过的依赖整层搬过来。两个阶段是同一个基础镜像，
# site-packages 的路径与 Python 版本完全一致
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

WORKDIR /app

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
