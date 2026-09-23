FROM python:3.11-slim

# 不写 .pyc、日志直出
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY app ./app

# SQLite 数据目录：由容器内非 root 用户持有，便于挂载卷
RUN mkdir -p /data && useradd -r -u 10001 appuser \
    && chown -R appuser:appuser /data /app
USER appuser

ENV DATABASE_PATH=/data/resolutions.db \
    SOLVER_MAX_ATTEMPTS=100000

EXPOSE 8080

# 容器启动即完成依赖（镜像内已安装）与数据库初始化（lifespan 建库建表）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
