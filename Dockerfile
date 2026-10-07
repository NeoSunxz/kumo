# syntax=docker/dockerfile:1
FROM python:3.13-slim

# 国内网络访问 PyPI 不稳定时, 构建时传 --build-arg PIP_INDEX_URL=<镜像源>
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    KUMO_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

EXPOSE 8115
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8115/health')"

CMD ["python", "-m", "kumo"]
