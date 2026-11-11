# RAHBAR ui image: static files behind nginx, non-root, read-only root filesystem.
# nginx-unprivileged listens on 8080 and runs as uid 101; nginx.conf writes only under
# /tmp, which the Deployment mounts as an emptyDir. No build step: index.html is plain
# HTML, so there is no Node stage to leave behind.
#
# Base: the stable 1.30 branch on Alpine 3.24, "-slim" variant (fewer packages, so fewer
# CVEs to carry), pinned by tag and digest. The previous base (1.27.4-alpine) was on
# Alpine 3.21, which is end-of-life; Trivy found 67 fixable HIGH/CRITICAL issues in it.
# `apk upgrade` applies Alpine fixes published after this digest was built.

FROM nginxinc/nginx-unprivileged:1.31.7-alpine3.24-slim@sha256:a8cb0f33d51fa107c96fd2bd3a71aaf564ec131e9d2bdf8700944f0ed8b170d7
USER root
RUN apk upgrade --no-cache
COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html
USER 101
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
