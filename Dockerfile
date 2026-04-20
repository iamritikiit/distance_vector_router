# =============================================================
# Dockerfile for Distance-Vector Routing Daemon
# =============================================================
# Uses Alpine-based Python for small image size.
# Installs iproute2 for `ip route` / `ip addr` commands.
# Grants NET_ADMIN capability (set in docker-compose.yml)
# so the daemon can modify the Linux routing table.
# =============================================================

FROM python:3.10-alpine

# iproute2 provides `ip` command (ip route, ip addr)
RUN apk add --no-cache iproute2

WORKDIR /app
COPY router.py .

# Expose RIP-like UDP port
EXPOSE 5000/udp

CMD ["python3", "-u", "router.py"]
