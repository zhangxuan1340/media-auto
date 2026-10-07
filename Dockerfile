# MediaAuto Web 控制台 + 同步作业
# 本地未做 docker build 验证(本机无 Docker);运行时依赖只有 requirements.txt。
FROM python:3.12-slim

# ── 国内构建加速(CN 网络下必需, 否则 apt/pip 直连官方源极慢)────────────────
# apt: 把 Debian 官方源(deb.debian.org / security.debian.org)换成阿里云镜像。
#   python:3.12-slim 是 Debian bookworm, 源文件用 deb822 格式
#   (/etc/apt/sources.list.d/debian.sources);旧格式在 /etc/apt/sources.list。
#   两个路径都 sed 一遍(不存在的忽略), 保证 bookworm 与旧版都覆盖到。
RUN sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g; s|http://security.debian.org|http://mirrors.aliyun.com/debian-security|g' \
        /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true

# pip: 换成清华 TUNA 镜像 + 关掉版本检查(少一次网络往返)。
#   阿里云 pip 镜像也很快, 可替换为 https://mirrors.aliyun.com/pypi/simple
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_DISABLE_PIP_VERSION_CHECK=1

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
