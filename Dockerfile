# MediaAuto Web 控制台 + 同步作业
# 本地未做 docker build 验证(本机无 Docker);运行时依赖只有 requirements.txt。
FROM python:3.12-slim

# mediainfo / ffmpeg(ffprobe): NFO <fileinfo> 探测用, 属可选能力, 缺了也会优雅降级
RUN apt-get update && apt-get install -y --no-install-recommends \
        mediainfo \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV MEDIA_AUTO_DIR=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8787

# 探活用 GET /(本项目没有 /api/health)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "server.main"]
