#!/bin/sh
set -eu

VERSION=v1.19.31
ASSET="mihomo-linux-amd64-v1-${VERSION}.gz"
URL="https://github.com/MetaCubeX/mihomo/releases/download/${VERSION}/${ASSET}"
EXPECTED_SHA256=d4304c546c3cddcb6fafd4b4fddb0ba1a95ffa36606fda56d75db2e59ad24114

command -v curl >/dev/null
command -v sha256sum >/dev/null
command -v gzip >/dev/null
test -r /etc/ssl/certs/ca-certificates.crt

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT HUP INT TERM
curl --fail --location --retry 3 --silent --show-error "$URL" --output "$tmp"
printf '%s  %s\n' "$EXPECTED_SHA256" "$tmp" | sha256sum --check --status
gzip --decompress --stdout "$tmp" > mihomo
chmod 755 mihomo
cp /etc/ssl/certs/ca-certificates.crt ca-certificates.crt
chmod 644 ca-certificates.crt
printf 'Prepared official Mihomo %s runtime files.\n' "$VERSION"
