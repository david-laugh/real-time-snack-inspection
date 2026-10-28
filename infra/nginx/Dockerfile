# Nginx inference load balancer (C2 -> Nginx -> C3 replicas)
# nginx >= 1.27.3 is required for the `resolve` parameter in upstream servers,
# which lets nginx start even when scale-out replicas (c3-inference-2/3) are not running.
FROM nginx:1.28-alpine

COPY nginx.conf /etc/nginx/nginx.conf

EXPOSE 8080
