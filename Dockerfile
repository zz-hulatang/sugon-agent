# 使用 Docker 官方在 ECR 发布的镜像，避免依赖 Docker Hub 的连通性。
ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    OA_BROWSER_CHANNEL="" \
    DISPLAY=:99

WORKDIR /app

# 登录使用有界面的 Chromium；通过 noVNC 完成人工验证码/扫码。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        xvfb x11-utils x11vnc fluxbox novnc websockify fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN python -m pip install . \
    && python -m playwright install --with-deps chromium --no-shell \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /opt/playwright

COPY apps/ ./apps/
COPY main.py ./
COPY docker/entrypoint.sh /usr/local/bin/oa-entrypoint
RUN useradd --create-home --uid 10001 app \
    && chmod +x /usr/local/bin/oa-entrypoint

USER app
EXPOSE 8501 6080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)" || exit 1

ENTRYPOINT ["/usr/local/bin/oa-entrypoint"]
CMD ["python", "-m", "streamlit", "run", "apps/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false"]
