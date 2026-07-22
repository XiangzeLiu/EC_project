# Caddy Windows Deployment Notes

## Runtime layout

SM and TS each own an independent runtime directory in production:

```text
caddy/
  caddy.exe
  Caddyfile
  data/
  config/
  logs/
  caddy.pid
```

The Python application validates, reloads, or starts Caddy. The Windows batch
files only provide production environment variables and launch the application.

## SM

- Public entry: `https://scjrdomain.com`
- Local upstream: `http://127.0.0.1:8800`
- Local Caddy admin endpoint: `127.0.0.1:2019`
- Source runtime executable: `Server_manager/caddy/caddy.exe`
- Packaged runtime executable: `caddy/caddy.exe` beside `ServerManager.exe`

Direct PyCharm execution and the packaged EXE use the same FastAPI startup
hook. Set `SM_CADDY_REQUIRED=1` in production so a missing or invalid Caddy
runtime prevents SM from entering service.

## TS

- Public entry: `wss://<assigned-domain>/ws`
- Local upstream: `http://127.0.0.1:8900`
- Local Caddy admin endpoint: `127.0.0.1:2020`
- Source runtime executable: `Trader_Server/caddy/caddy.exe`
- Packaged runtime executable: `caddy/caddy.exe` beside `TraderServer.exe`

An unregistered TS does not start Caddy. After SM approval, TS saves the
assigned domain, generates `caddy/Caddyfile`, and starts or reloads Caddy.
An already registered TS repeats the same idempotent check during startup.

## Persistent data

The application sets these values for the Caddy subprocess:

```text
XDG_DATA_HOME=<runtime>/caddy/data
XDG_CONFIG_HOME=<runtime>/caddy/config
```

Do not delete or replace these directories during an application upgrade.
They contain automatic certificate and Caddy state data.

## Process behavior

Caddy runs as a detached `caddy run` process. Closing SM or TS does not stop
Caddy. The next application start validates the desired Caddyfile and reloads
the existing process through its loopback-only admin endpoint.

Manual stop commands:

```bat
caddy\caddy.exe stop --address 127.0.0.1:2019
caddy\caddy.exe stop --address 127.0.0.1:2020
```

## Same-machine development

SM and TS production nodes are expected to be on different servers. When both
run on one development machine, only one Caddy instance can own ports 80/443.
A typical local setup is:

```bat
set SM_CADDY_AUTO_MANAGE=1
set TS_CADDY_AUTO_MANAGE=0
```

The local TS then remains available at `ws://127.0.0.1:8900/ws`.

## Firewall and certificates

- Open public TCP ports 80 and 443.
- Do not expose 8800, 8900, 2019, or 2020 publicly.
- DNS must point each domain to the correct server before certificate issuance.
- Caddy obtains and renews the individual SM and TS certificates automatically.

## Temporary direct-IP testing

For short tests without Caddy, explicitly disable automatic management and use
direct local or public test addresses. This is not the production target.

```bat
set SM_CADDY_AUTO_MANAGE=0
set TS_CADDY_AUTO_MANAGE=0
set SERVER_HOST=0.0.0.0
set TS_BIND_HOST=0.0.0.0
```
