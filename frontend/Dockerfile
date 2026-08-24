FROM node:22.23.2-alpine AS dependencies
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

FROM node:22.23.2-alpine AS builder
WORKDIR /app
COPY --from=dependencies /app/node_modules ./node_modules
COPY frontend ./
RUN npm run build

FROM node:22.23.2-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production HOSTNAME=0.0.0.0 PORT=3000
RUN addgroup --system --gid 10001 sponsorflow \
    && adduser --system --uid 10001 --ingroup sponsorflow sponsorflow
COPY --from=builder --chown=10001:10001 /app/.next/standalone ./
USER 10001:10001
EXPOSE 3000
CMD ["node", "server.js"]
