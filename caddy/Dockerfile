FROM caddy:2.11.4-builder-alpine AS builder

RUN xcaddy build v2.11.4 \
    --with github.com/caddy-dns/porkbun@v0.3.1

FROM caddy:2.11.4-alpine

COPY --from=builder /usr/bin/caddy /usr/bin/caddy
