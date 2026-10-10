#!/bin/sh
# Renders the nginx configuration for the stack's ingress mode, before nginx
# starts. Mounted into the stock nginx image as
# /docker-entrypoint.d/15-istota-ingress.sh, which the image's own entrypoint
# runs (and stops on, if this exits non-zero) ahead of `nginx`.
#
# INGRESS (vm.env) picks the server blocks; the location blocks are always
# istota.conf.template:
#
#   local    listen-local.conf
#   proxied  listen-proxied.conf, allowing only UPSTREAM_PROXY, plain or with
#            TLS from files
#   direct   direct-http.conf (the ACME challenge and the redirect), plus
#            listen-direct.conf once a certificate exists. Before the first
#            certificate nginx cannot load a 443 block, so it serves port 80
#            alone until certbot's deploy hook restarts it.
#
# Everything an operator writes (DOMAIN, UPSTREAM_PROXY) is checked for the
# characters it may contain before it is substituted into the configuration.
# tests/image/test_nginx_ingress.py renders every mode with this script and
# runs `nginx -t` on the result.
set -eu

SRC=/etc/istota-nginx
OUT=/etc/nginx/istota
CONF=/etc/nginx/conf.d/default.conf
# The full test tier's Nextcloud fixture swaps in its own locations here.
ROOT_LOCATIONS="${ISTOTA_NGINX_ROOT_LOCATIONS:-$SRC/root-locations.conf}"

INGRESS="${INGRESS:-local}"
TLS_CERT_SOURCE="${TLS_CERT_SOURCE:-}"
WEB_PORT="${WEB_PORT:-8766}"
WEBHOOKS_PORT="${WEBHOOKS_PORT:-8765}"
NGINX_CLIENT_MAX_BODY_SIZE="${NGINX_CLIENT_MAX_BODY_SIZE:-512M}"
# server_name takes no port; DOMAIN may carry one in local mode.
DOMAIN="${DOMAIN:-localhost}"
DOMAIN="${DOMAIN%%:*}"
TLS_CERT_DIR=""
PROXIED_LISTEN=""
UPSTREAM_ALLOW=""

log() { echo "[istota-ingress] $*"; }
refuse() { echo "[istota-ingress] REFUSE: $*" >&2; exit 1; }

case "$DOMAIN" in
    *[!A-Za-z0-9.-]*|"") refuse "DOMAIN '${DOMAIN}' is not a hostname." ;;
esac
case "$WEB_PORT$WEBHOOKS_PORT" in
    *[!0-9]*) refuse "WEB_PORT and WEBHOOKS_PORT must be numbers." ;;
esac
case "$NGINX_CLIENT_MAX_BODY_SIZE" in
    *[!0-9kKmMgG]*|"") refuse "NGINX_CLIENT_MAX_BODY_SIZE '${NGINX_CLIENT_MAX_BODY_SIZE}' is not a size." ;;
esac

render() {
    # An explicit list, so nginx's own variables pass through untouched.
    envsubst '${DOMAIN} ${WEB_PORT} ${WEBHOOKS_PORT} ${NGINX_CLIENT_MAX_BODY_SIZE} ${TLS_CERT_DIR} ${PROXIED_LISTEN} ${UPSTREAM_ALLOW}' < "$SRC/$1"
}

have_cert() {
    [ -s "$TLS_CERT_DIR/fullchain.pem" ] && [ -s "$TLS_CERT_DIR/privkey.pem" ]
}

case "$TLS_CERT_SOURCE" in
    acme) TLS_CERT_DIR="/etc/nginx/certs/live/${DOMAIN}" ;;
    files) TLS_CERT_DIR="/etc/nginx/certs" ;;
    "") ;;
    *) refuse "TLS_CERT_SOURCE '${TLS_CERT_SOURCE}' is neither acme nor files." ;;
esac
export DOMAIN WEB_PORT WEBHOOKS_PORT NGINX_CLIENT_MAX_BODY_SIZE TLS_CERT_DIR

mkdir -p "$OUT"
# http level: the websocket upgrade map, and the scheme an upstream proxy
# reports (https when it sends none, since proxied always sits behind TLS).
cat > "$CONF" <<'MAPS'
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

map $http_x_forwarded_proto $istota_upstream_proto {
    default $http_x_forwarded_proto;
    ''      https;
}

MAPS

case "$INGRESS" in
    local)
        [ -z "$TLS_CERT_SOURCE" ] || refuse "INGRESS=local terminates no TLS; leave TLS_CERT_SOURCE empty."
        render listen-local.conf >> "$CONF"
        ;;
    proxied)
        [ -n "${UPSTREAM_PROXY:-}" ] || refuse "INGRESS=proxied needs UPSTREAM_PROXY, the reverse proxy's address(es); without it any client could forge X-Forwarded-For."
        for entry in $(echo "$UPSTREAM_PROXY" | tr ',' ' '); do
            case "$entry" in
                *[!0-9A-Fa-f.:/]*) refuse "UPSTREAM_PROXY entry '${entry}' is not an address or CIDR." ;;
                0.0.0.0/0|::/0) refuse "UPSTREAM_PROXY entry '${entry}' allows every address." ;;
            esac
            UPSTREAM_ALLOW="${UPSTREAM_ALLOW}allow ${entry}; "
        done
        case "$TLS_CERT_SOURCE" in
            files)
                have_cert || refuse "TLS_CERT_SOURCE=files and there is no fullchain.pem and privkey.pem in /srv/istota/certs."
                PROXIED_LISTEN="listen 443 ssl default_server; include ${OUT}/tls.conf;"
                ;;
            "") PROXIED_LISTEN="listen 80 default_server;" ;;
            *) refuse "INGRESS=proxied takes certificates from files or none; ACME needs the public name to point here." ;;
        esac
        export PROXIED_LISTEN UPSTREAM_ALLOW
        render listen-proxied.conf >> "$CONF"
        ;;
    direct)
        [ -n "$TLS_CERT_SOURCE" ] || refuse "INGRESS=direct needs TLS_CERT_SOURCE (acme or files)."
        render direct-http.conf >> "$CONF"
        if have_cert; then
            render listen-direct.conf >> "$CONF"
        else
            log "no certificate in ${TLS_CERT_DIR} yet; serving the ACME challenge and the redirect on port 80 only."
        fi
        ;;
    *)
        refuse "INGRESS '${INGRESS}' is not one of direct, proxied, local."
        ;;
esac

render tls.conf > "$OUT/tls.conf"
render istota.conf.template > "$OUT/server.conf"
cp "$ROOT_LOCATIONS" "$OUT/root.conf"
log "INGRESS=${INGRESS} rendered for ${DOMAIN}."
