# RAHBAR ui image: static files behind nginx, non-root, read-only root filesystem.
# nginx-unprivileged already runs as uid 101 and listens on 8080; nginx.conf writes only
# under /tmp, which the Deployment mounts as an emptyDir. No build step: index.html is
# plain HTML, so there is no Node stage to leave behind.

FROM nginxinc/nginx-unprivileged:1.27.4-alpine
COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html
USER 101
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
