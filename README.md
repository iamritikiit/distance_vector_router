# Distance-Vector Routing Daemon — Docker Test Setup

## Overview

This project implements a **RIP-like Distance-Vector routing protocol** using the **Bellman-Ford algorithm** in Python. The Docker setup lets you test 3 routers on a single machine with zero manual IP configuration.

## Topology

```
┌──────────┐     net_ab        ┌──────────┐     net_bc        ┌──────────┐
│ Router A │◄──────────────────►│ Router B │◄──────────────────►│ Router C │
│ 10.10.1.10│   10.10.1.0/24   │10.10.1.20│   10.10.2.0/24   │10.10.2.30│
└──────────┘                   │10.10.2.20│                   └──────────┘
                               └──────────┘
```

- **Router A** — only on `net_ab`, neighbor is B
- **Router B** — on both `net_ab` and `net_bc` (transit router), neighbors are A and C
- **Router C** — only on `net_bc`, neighbor is B

## Prerequisites

**Install Docker Desktop** (if not already installed):

```bash
# macOS — using Homebrew
brew install --cask docker

# Then open Docker Desktop from Applications and wait for it to start.
# Verify installation:
docker --version
docker-compose --version
```

If you don't have Homebrew, download Docker Desktop from: https://www.docker.com/products/docker-desktop/

## Files

| File | Purpose |
|------|---------|
| `router.py` | The DV routing daemon (Bellman-Ford + Poison Reverse + Triggered Updates) |
| `Dockerfile` | Builds an Alpine Linux container with Python 3.10 and iproute2 |
| `docker-compose.yml` | Defines the 3-router topology with two bridge networks |

## Quick Start

### 1. Build and Start All Routers

```bash
cd /Users/ritik/Desktop/Assignment4_cn
docker-compose up --build
```

This will:
- Build the Docker image
- Create two networks (`net_ab`, `net_bc`)
- Start all 3 routers
- Show interleaved logs from all routers

### 2. Watch Logs (in a separate terminal)

```bash
# All routers
docker-compose logs -f

# Specific router only
docker-compose logs -f router_a
docker-compose logs -f router_b
docker-compose logs -f router_c
```

## What to Expect

### Phase 1 — Startup (0-5 seconds)
Each router:
1. Auto-detects its own IP address
2. Resolves neighbor hostnames via Docker DNS (e.g., `router_b` → `10.10.1.20`)
3. Discovers directly-connected subnets (distance 0)
4. Begins broadcasting DV updates

### Phase 2 — Convergence (5-15 seconds)
You will see in logs:
```
ADD     10.10.2.0/24 via 10.10.1.20 distance 1       ← Router A learns about net_bc via B
ADD     10.10.1.0/24 via 10.10.2.20 distance 1       ← Router C learns about net_ab via B
TRIGGER sent immediate update to ...                   ← Triggered updates fire
```

### Phase 3 — Stable (after ~15 seconds)
```
✔ Routing table CONVERGED (stable for 3 cycles)
STABLE  No changes from update by ... (network stable)
```

### Expected Routing Tables After Convergence

**Router A:**
| Subnet | Distance | Next Hop | Status |
|--------|----------|----------|--------|
| 10.10.1.0/24 | 0 | 0.0.0.0 | connected |
| 10.10.2.0/24 | 1 | 10.10.1.20 | learned |

**Router B:**
| Subnet | Distance | Next Hop | Status |
|--------|----------|----------|--------|
| 10.10.1.0/24 | 0 | 0.0.0.0 | connected |
| 10.10.2.0/24 | 0 | 0.0.0.0 | connected |

**Router C:**
| Subnet | Distance | Next Hop | Status |
|--------|----------|----------|--------|
| 10.10.2.0/24 | 0 | 0.0.0.0 | connected |
| 10.10.1.0/24 | 1 | 10.10.2.20 | learned |

## Testing Scenarios

### Test 1: Verify Normal Convergence
```bash
docker-compose up --build
# Wait 15-20 seconds
# Look for "CONVERGED" messages in logs
```

### Test 2: Simulate Router Failure
```bash
# In a separate terminal, stop Router C:
docker-compose stop router_c

# Watch logs — within 15 seconds you should see:
#   TIMEOUT neighbor 10.10.2.30 has not responded in 15s — poisoning routes
#   POISON  10.10.2.0/24 via 10.10.2.30 (neighbor timeout, holding for 2 cycles)
#   TRIGGER update sent to all neighbors
#   FLUSH   10.10.2.0/24 removed after 2 poison-hold cycles
```

### Test 3: Verify Reconvergence After Recovery
```bash
# Bring Router C back:
docker-compose start router_c

# Watch logs — within a few seconds:
#   ADD     10.10.2.0/24 via 10.10.2.30 distance 1    ← route re-learned
#   ✔ Routing table CONVERGED
```

### Test 4: Verify Kernel Routing Table
```bash
# Check actual Linux routes inside a container:
docker exec router_a ip route
docker exec router_c ip route

# You should see entries like:
#   10.10.2.0/24 via 10.10.1.20 dev eth0
```

## Tear Down

```bash
docker-compose down
```

This removes all containers and networks.

## Features Demonstrated

| Feature | How to Verify |
|---------|---------------|
| **Bellman-Ford** | Watch `ADD`/`UPDATE` messages as routes propagate |
| **Split Horizon + Poison Reverse** | Router B never advertises A's subnet back to A with valid metric |
| **Triggered Updates** | `TRIGGER` log lines appear immediately after any change |
| **Neighbor Failure Detection** | Stop a router → `TIMEOUT` + `POISON` logs appear |
| **Two-Phase Route Withdrawal** | Poisoned routes held for 2 cycles before `FLUSH` |
| **Convergence Detection** | `✔ Routing table CONVERGED` message after stability |
| **Kernel FIB Integration** | `ip route` inside containers shows learned routes |

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `command not found: docker` | Install Docker Desktop (see Prerequisites) |
| `Cannot connect to Docker daemon` | Open Docker Desktop app and wait for it to start |
| `port already in use` | No host ports are exposed, this shouldn't happen |
| DNS resolution fails | Containers start in parallel; retry logic handles this automatically |
