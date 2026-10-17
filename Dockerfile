FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY product/pyproject.toml product/LICENSE product/requirements-hosted.txt ./product/
COPY product/src ./product/src
RUN python -m pip install -r product/requirements-hosted.txt && python -m pip install --no-deps ./product
ENV AFTERWARD_HOSTED=1 AFTERWARD_DATA_DIR=/data/afterward-workspace
EXPOSE 8080
CMD ["python", "-m", "carerelay.app"]
