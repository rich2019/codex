FROM scratch

# Official MetaCubeX/mihomo v1.19.31 linux-amd64-v1 binary.
# SHA256: 12d97b7b7fa22cb4456e62c6e35db4be952dbb1c53eacaa9ef437c95d5068a9d
COPY --chmod=755 mihomo /usr/local/bin/mihomo
RUN ["/usr/local/bin/mihomo", "-v"]

COPY ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY ca-certificates.crt /etc/ssl/cert.pem

ENTRYPOINT ["/usr/local/bin/mihomo"]
