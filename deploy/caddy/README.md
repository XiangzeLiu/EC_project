# Caddy Windows Deployment Notes

## Goal

Use Caddy as the public HTTPS/WSS entry on each Windows server.

- SM server: public `https://sm.yourdomain.com` -> local `http://127.0.0.1:8800`
- Each TS server: public `wss://sg-01.ts.yourdomain.com/ws` -> local `http://127.0.0.1:8900/ws`
- Public firewall opens only `80/tcp` and `443/tcp`.
- Do not expose `8800/tcp` or `8900/tcp` to the public internet.

## SM Server

1. Put `caddy.exe` and a copied `Caddyfile.sm` on the SM Windows server.
2. Replace `sm.yourdomain.com` with the real SM domain.
3. Point DNS `A` record for the SM domain to the SM server public IP.
4. Start ServerManager with:

```bat
set SERVER_HOST=127.0.0.1
set SERVER_PORT=8800
ServerManager.exe
```

5. Start Caddy:

```bat
caddy run --config Caddyfile.sm
```

## TS Server

1. Put `caddy.exe` and a copied `Caddyfile.ts` on each TS Windows server.
2. Replace `sg-01.ts.yourdomain.com` with this TS server's real domain.
3. Point DNS `A` record for this TS domain to this TS server public IP.
4. Start TraderServer with:

```bat
set TS_BIND_HOST=127.0.0.1
set TS_WS_PORT=8900
set TS_MANAGER_URL=https://sm.yourdomain.com
set TS_PUBLIC_ENDPOINT=wss://sg-01.ts.yourdomain.com/ws
TraderServer.exe
```

5. Start Caddy:

```bat
caddy run --config Caddyfile.ts
```

## Certificate Notes

Caddy obtains and renews HTTPS certificates automatically. Keep the Caddy data
directory on disk and do not delete it during upgrades. If a TS server IP
changes, update the DNS `A` record and keep the same TS domain where possible.

## Temporary Direct IP Testing

For short functional tests before domains are ready, you may run without Caddy:

```bat
set SERVER_HOST=0.0.0.0
set TS_BIND_HOST=0.0.0.0
```

Then use `http://<SM_IP>:8800` and `ws://<TS_IP>:8900/ws`. This is only for
temporary testing, not the production security target.
