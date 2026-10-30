# Atlas Research Agent —— CompassRAG 检索服务（FastAPI + Agent）
# 说明：镜像只装轻量依赖（BM25 + API）。稠密检索需要 `.[dense]`（sentence-transformers，
# 镜像会大 1-2GB），实体图需要预先构建好的索引；两者都通过挂载 data/ 与 runs/ 提供。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# 单仓双包：atlas 运行时 + compassrag 检索层（镜像只启用检索服务，但打包需要两个包）
COPY pyproject.toml README.md ./
COPY atlas ./atlas
COPY compassrag ./compassrag
COPY scripts ./scripts
COPY configs ./configs

RUN pip install --upgrade pip && pip install -e ".[api]"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 默认用基准索引；挂载 data/ 才有语料。上传文档模式改用 --index-source ingest。
CMD ["python", "scripts/serve_api.py", "--host", "0.0.0.0", "--port", "8000"]
