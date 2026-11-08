# RAHBAR api image. Build context: api/
# Two stages: wheels are built in the first, only the installed packages and the app
# reach the final image. Runs as a non-root user; the start command is exec-form, so
# SIGTERM from Kubernetes reaches uvicorn (PID 1) and in-flight requests can finish.

FROM python:3.12.10-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

FROM python:3.12.10-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --system --gid 10001 rahbar \
 && useradd --system --uid 10001 --gid rahbar --no-create-home rahbar
COPY --from=build /install /usr/local
WORKDIR /app
COPY app/ ./app/
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20"]
