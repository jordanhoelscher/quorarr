FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
# Baked in as the pre-config fallback the splash paints before /api/config
# answers; the running instance's real name comes from APP_NAME at runtime.
ARG APP_NAME=Quorarr
ENV APP_NAME=${APP_NAME}
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/pensieve ./pensieve
RUN pip install --no-cache-dir .
COPY --from=frontend /build/dist /app/static
ENV STATIC_DIR=/app/static
# Which proxies may set X-Forwarded-For. '*' means "trust whatever reached
# me", which is correct ONLY when this container publishes no port and is
# reachable exclusively through a reverse proxy on a private Docker network
# -- the documented deployment. Publish a port, or put an untrusted hop in
# front, and this must be narrowed to the proxy's address, or a client can
# spoof its own IP past the login rate limiter. Overridable per deployment:
# uvicorn reads this variable when the CLI flag is absent.
ENV FORWARDED_ALLOW_IPS=*
USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"]
CMD ["uvicorn", "pensieve.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
