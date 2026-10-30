# TravelBot UI: static files served by nginx, which also proxies /api/* to the backend.
FROM nginx:1.27.5-alpine
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY index.html styles.css script.js /usr/share/nginx/html/
COPY fonts /usr/share/nginx/html/fonts
EXPOSE 8501
