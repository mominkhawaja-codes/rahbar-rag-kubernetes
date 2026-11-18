# RAHBAR api image. Build context: api/
# Two stages: wheels are built in the first, only the installed packages and the app
# reach the final image. Runs as a non-root user; the start command is exec-form, so
# SIGTERM from Kubernetes reaches uvicorn (PID 1) and in-flight requests can finish.
#
# The base is pinned by tag AND digest, so a build is reproducible; Dependabot
# (.github/dependabot.yml) proposes the next digest. `apt-get upgrade` picks up Debian
# security fixes published after that digest was built; Trivy in CI checks the result.

FROM python:3.15.2-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9 AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

FROM python:3.15.2-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 rahbar \
 && useradd --system --uid 10001 --gid rahbar --no-create-home rahbar
COPY --from=build /install /usr/local
WORKDIR /app
COPY app/ ./app/
USER 10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20"]
