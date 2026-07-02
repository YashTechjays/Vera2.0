# Build from vera-frontend/:
#   docker build --build-arg VITE_API_BASE_URL=/api/v1 -t vera-frontend .
#
# The SPA is served by nginx, which also reverse-proxies /api to the
# control-plane container so the browser stays same-origin (no CORS) and works
# over plain HTTP — see nginx.conf. VITE_API_BASE_URL is baked at build time
# (Vite inlines import.meta.env.*), so it is set per environment by the build.

FROM node:22-bookworm-slim AS builder
WORKDIR /app

# Install against the lockfile first so the layer caches across source changes.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Same-origin API path by default; the pipeline overrides per environment.
ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build

FROM nginx:1.27-alpine
# Drop nginx's default site; ship our SPA + /api proxy config.
RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/vera.conf
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
