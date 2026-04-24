import json
import os
import socket
import subprocess
import threading
import time
import logging
import ipaddress

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

UDP_PORT           = 5000                 # Protocol port (all routers)
BROADCAST_INTERVAL = 2                    # Seconds between periodic updates
MAX_DISTANCE       = 16                   # Infinity metric (RIP convention)
NEIGHBOR_TIMEOUT   = 12                   # Seconds before declaring a neighbor dead
BUFFER_SIZE        = 65535                # Max UDP datagram we'll accept
DV_VERSION         = 1                    # Protocol version tag (integer)
CONVERGE_CYCLES    = 3                    # Stable cycles before declaring convergence
POISON_HOLD_CYCLES = 3                    # How many broadcast cycles a poisoned route
                                          # is kept (at distance 16) before removal
LINK_DOWN_THRESHOLD = 2                   # Consecutive interface-check misses before
                                          # declaring a connected link as down

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dv-router")

# ---------------------------------------------------------------------------
# Global state  (protected by `table_lock`)
# ---------------------------------------------------------------------------

# routing_table : { "subnet_cidr" : [distance, next_hop] }
#   distance  = 0 for directly connected, 1..15 for learned, 16 = unreachable
#   next_hop  = "0.0.0.0" for directly connected subnets
routing_table: dict[str, list] = {}

# Track when we last heard from each neighbor so we can detect failures.
# neighbor_last_seen : { "neighbor_ip" : float(timestamp) }
neighbor_last_seen: dict[str, float] = {}

# Poisoned routes awaiting garbage collection.
# poison_hold : { "subnet" : int(remaining_broadcast_cycles) }
# When a route is poisoned (set to 16), it stays in the table for
# POISON_HOLD_CYCLES broadcasts so that every neighbor learns about
# the withdrawal.  After the counter reaches 0 the route is removed.
poison_hold: dict[str, int] = {}

# Link-down debounce counters.  Before declaring a directly-connected
# subnet as "down", we require LINK_DOWN_THRESHOLD consecutive
# interface-check misses.  This prevents transient kernel/Docker
# glitches from falsely poisoning connected routes.
# link_miss_count : { "subnet" : int(consecutive_misses) }
link_miss_count: dict[str, int] = {}

# Convergence tracking: counts consecutive broadcast cycles during
# which no routing table change was observed.
stable_cycles: int = 0
converged_announced: bool = False

# Thread-safety lock for all shared state mutations
table_lock = threading.Lock()

# Shared UDP socket for both periodic broadcasts and triggered updates.
# Created once in main(), reused everywhere.  Protected by its own lock
# so that the broadcast thread and the listener thread (which calls
# trigger_update via update_logic) never interleave sendto() calls.
_send_sock: socket.socket | None = None
_send_sock_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Environment bootstrap
# ---------------------------------------------------------------------------

def _resolve_neighbors(raw: list[str], retries: int = 10, delay: float = 2.0) -> list[str]:
    """
    Resolve a list of neighbor identifiers (hostnames OR IPs) to IPv4
    addresses.  Retries with back-off so that Docker DNS has time to
    register all containers at startup.

    This is what makes `NEIGHBORS=router_b,router_c` (container names)
    work transparently — Docker's embedded DNS resolves them to the
    correct container IPs on the shared network.
    """
    resolved: list[str] = []
    for entry in raw:
        for attempt in range(1, retries + 1):
            try:
                ip = socket.gethostbyname(entry)
                logger.info("DNS     %s → %s (attempt %d)", entry, ip, attempt)
                resolved.append(ip)
                break
            except socket.gaierror:
                if attempt == retries:
                    logger.error("DNS     failed to resolve '%s' after %d attempts", entry, retries)
                else:
                    logger.warning("DNS     '%s' not ready, retrying in %.0fs… (%d/%d)", entry, delay, attempt, retries)
                    time.sleep(delay)
    return resolved


def _detect_my_ip() -> str:
    """
    Auto-detect this host's primary non-loopback IPv4 address by
    parsing `ip -o -4 addr show`.  Used as fallback when MY_IP is
    not explicitly set (common in Docker Compose where the IP is
    assigned dynamically).
    """
    try:
        output = subprocess.check_output(["ip", "-o", "-4", "addr", "show"], text=True)
        for line in output.strip().splitlines():
            parts = line.split()
            for i, token in enumerate(parts):
                if token == "inet" and i + 1 < len(parts):
                    addr = parts[i + 1].split("/")[0]
                    if not addr.startswith("127."):
                        return addr
    except Exception as exc:
        logger.error("Failed to auto-detect IP: %s", exc)
    return ""


# --- Read env vars ---
MY_IP = os.environ.get("MY_IP", "")
_raw_neighbors = [
    n.strip()
    for n in os.environ.get("NEIGHBORS", "").split(",")
    if n.strip()
]

# Auto-detect MY_IP if not explicitly provided
if not MY_IP:
    MY_IP = _detect_my_ip()
    if MY_IP:
        logger.info("AUTO-IP detected own IP as %s", MY_IP)
    else:
        logger.error("MY_IP not set and auto-detection failed. Exiting.")
        raise SystemExit(1)

# Resolve neighbor hostnames/IPs (handles Docker container names via DNS)
NEIGHBORS = _resolve_neighbors(_raw_neighbors) if _raw_neighbors else []
if not NEIGHBORS:
    logger.warning("NEIGHBORS is empty — this router has no peers.")


# ---------------------------------------------------------------------------
# Helper: discover directly-connected subnets via `ip addr`
# ---------------------------------------------------------------------------

def discover_connected_subnets() -> dict[str, str]:
    """
    Parse the output of `ip -o addr show` to find every IPv4 subnet
    that this router is directly attached to.

    Returns a dict mapping CIDR networks to their interface name:
      {"10.0.1.0/24": "eth0"}

    Retries once on empty result to guard against transient kernel
    races (e.g. during Docker network attach/detach).
    """
    for attempt in range(2):
        subnets: dict[str, str] = {}
        try:
            output = subprocess.check_output(
                ["ip", "-o", "-4", "addr", "show"],
                text=True,
            )
            for line in output.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    dev_name = parts[1]
                    for i, token in enumerate(parts):
                        if token == "inet" and i + 1 < len(parts):
                            cidr = parts[i + 1]            # e.g. "10.0.1.2/24"
                            iface = ipaddress.IPv4Interface(cidr)
                            network = str(iface.network)    # e.g. "10.0.1.0/24"
                            # Skip loopback
                            if network.startswith("127."):
                                continue
                            subnets[network] = dev_name
        except Exception as exc:
            logger.error("Failed to discover connected subnets: %s", exc)

        if subnets or attempt == 1:
            return subnets

        # Empty result on first attempt — retry after a brief pause
        time.sleep(0.2)

    return subnets


def is_on_my_subnet(ip_str: str) -> bool:
    """
    Check whether `ip_str` falls within any of our currently-connected
    subnets.  Used to validate that a packet sender is actually a
    directly-attached neighbor — not a stale route via Docker gateway
    cross-network routing.

    Returns True if the IP is on one of our connected subnets.
    """
    try:
        addr = ipaddress.IPv4Address(ip_str)
        connected = discover_connected_subnets()
        for subnet_cidr in connected:
            network = ipaddress.IPv4Network(subnet_cidr, strict=False)
            if addr in network:
                return True
    except (ValueError, Exception):
        pass
    return False


# ---------------------------------------------------------------------------
# Helper: apply a route to the Linux kernel FIB
# ---------------------------------------------------------------------------

def apply_route(subnet: str, next_hop: str) -> bool:
    """
    Install or replace a route in the Linux routing table using
    `ip route replace <subnet> via <next_hop>`.

    For directly-connected subnets (next_hop == "0.0.0.0") we skip
    because the kernel already has them.

    Returns True if the route was successfully installed (or skipped
    for connected subnets), False if the kernel rejected it.
    """
    if next_hop == "0.0.0.0":
        return True  # Kernel manages connected routes itself
    try:
        cmd = ["ip", "route", "replace", subnet, "via", next_hop]
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("KERNEL  ip route replace %s via %s", subnet, next_hop)
        return True
    except subprocess.CalledProcessError as exc:
        logger.error("ip route replace %s via %s FAILED: %s", subnet, next_hop, exc)
        return False


def remove_route(subnet: str) -> None:
    """
    Delete a route from the Linux FIB.
    Silently ignores errors (e.g. route already absent).

    SAFETY: Never removes a route for a currently-connected subnet.
    Deleting a kernel-managed connected route would break reachability
    on that interface, and the kernel does NOT automatically re-add it.
    """
    # --- Connected-route protection ---
    # Check if this subnet corresponds to an active interface.
    # If so, refuse to delete — the kernel needs this route.
    try:
        connected = discover_connected_subnets()
        if subnet in connected:
            logger.info(
                "KERNEL  SKIP ip route del %s — subnet is currently connected",
                subnet,
            )
            return
    except Exception:
        pass  # If discovery fails, proceed cautiously (still try to delete)

    try:
        cmd = ["ip", "route", "del", subnet]
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("KERNEL  ip route del %s", subnet)
    except subprocess.CalledProcessError:
        pass  # Route may not exist — that's fine


# ---------------------------------------------------------------------------
# Initialization: seed routing table with connected subnets
# ---------------------------------------------------------------------------

def initialize_routing_table() -> None:
    """
    Populate the routing table with all directly-connected subnets
    at distance 0 and next_hop 0.0.0.0 (meaning 'self / connected').
    """
    connected = discover_connected_subnets().keys()
    with table_lock:
        for subnet in connected:
            routing_table[subnet] = [0, "0.0.0.0"]
            logger.info("INIT    connected subnet %s → distance 0", subnet)
        # Also ensure every neighbor's /32 or containing subnet is present.
        # (They should be covered by the interface subnets above, but this
        #  is a safety net.)
    print_routing_table("Initial routing table")


# ---------------------------------------------------------------------------
# Pretty-print routing table
# ---------------------------------------------------------------------------

def print_routing_table(title: str = "Routing Table") -> None:
    """
    Log the full routing table in a human-readable tabular format.

    Displays each subnet with its distance and next-hop, along with
    status tags for unreachable / poisoned routes.  Used for both
    on-demand and periodic logging.
    """
    with table_lock:
        entries = dict(routing_table)
        held = dict(poison_hold)
    logger.info("┌── %s ──────────────────────────────────────────┐", title)
    logger.info("│  %-20s  %8s  %-16s  %s │", "Subnet", "Distance", "Next Hop", "Status")
    logger.info("│  %-20s  %8s  %-16s  %s │", "─" * 20, "─" * 8, "─" * 16, "─" * 12)
    for subnet in sorted(entries):
        dist, nh = entries[subnet]
        if dist >= MAX_DISTANCE:
            status = "POISONED"
        elif dist == 0 and nh == "0.0.0.0":
            status = "connected"
        else:
            status = "learned"
        logger.info("│  %-20s  %8d  %-16s  %-12s │", subnet, dist, nh, status)
    if not entries:
        logger.info("│  (empty)                                               │")
    logger.info("└──────────────────────────────────────────────────────────┘")


# ---------------------------------------------------------------------------
# Core DV logic
# ---------------------------------------------------------------------------

def build_dv_packet(target_neighbor: str | None = None) -> dict:
    """
    Build a DV-JSON advertisement packet.

    Applies **Split Horizon with Poison Reverse**:
    - Routes learned from `target_neighbor` are NOT omitted; instead
      they are advertised back with distance = MAX_DISTANCE (16).
    - This explicitly tells the neighbor "do NOT route through me for
      this destination", which is stronger than plain Split Horizon
      (which silently omits) and leads to faster convergence.

    Parameters
    ----------
    target_neighbor : str or None
        The IP of the neighbor we are sending this packet TO.
        If None, no poison-reverse is applied (plain advertisement).

    Returns
    -------
    dict   – ready-to-serialize DV-JSON packet
    """
    routes = []
    with table_lock:
        for subnet, (distance, next_hop) in routing_table.items():
            # --- Split Horizon with Poison Reverse ---
            # If we learned this route from the neighbor we're about to
            # send to, advertise it with distance = MAX_DISTANCE (16)
            # instead of omitting it.  This gives the neighbor an
            # explicit "unreachable" signal and prevents routing loops.
            if target_neighbor and next_hop == target_neighbor:
                routes.append({
                    "subnet": subnet,
                    "distance": MAX_DISTANCE,
                })
            else:
                routes.append({
                    "subnet": subnet,
                    "distance": distance,
                })

    packet = {
        "router_id": MY_IP,
        "version": DV_VERSION,
        "routes": routes,
    }
    return packet


def trigger_update() -> None:
    """
    Send an immediate (triggered) update to ALL neighbors.

    Called whenever the routing table changes — after an ADD, UPDATE,
    or POISON event in update_logic() or neighbor_timeout_checker().
    This implements RFC 2453 §3.10.1 "Triggered Updates" and ensures
    that topology changes propagate without waiting for the next
    periodic broadcast cycle, dramatically improving convergence time.

    Uses the shared _send_sock (with its own lock) so that triggered
    sends never interleave with periodic broadcasts.
    """
    if _send_sock is None:
        return  # Daemon hasn't started yet

    for neighbor in NEIGHBORS:
        packet = build_dv_packet(target_neighbor=neighbor)
        data = json.dumps(packet).encode("utf-8")
        try:
            with _send_sock_lock:
                _send_sock.sendto(data, (neighbor, UDP_PORT))
            logger.info(
                "TRIGGER sent immediate update to %s (%d routes)",
                neighbor,
                len(packet["routes"]),
            )
        except OSError as exc:
            logger.error("TRIGGER failed to %s: %s", neighbor, exc)

    logger.info("TRIGGER update sent to all neighbors")


def broadcast_updates() -> None:
    """
    Periodically send our routing table to every neighbor.

    Runs forever in a daemon thread.  Sends one UDP datagram per
    neighbor every BROADCAST_INTERVAL seconds.  Split Horizon with
    Poison Reverse is applied per-neighbor (see build_dv_packet).

    Also performs:
    - Periodic routing table logging (every cycle, for demo/report)
    - Garbage-collection of expired poisoned routes
    - Convergence detection (tracks stable_cycles counter)
    """
    global stable_cycles, converged_announced

    logger.info("BROADCAST thread started (interval=%ds)", BROADCAST_INTERVAL)

    while True:
        # ---- Send updates to all neighbors ----
        for neighbor in NEIGHBORS:
            packet = build_dv_packet(target_neighbor=neighbor)
            data = json.dumps(packet).encode("utf-8")
            try:
                with _send_sock_lock:
                    _send_sock.sendto(data, (neighbor, UDP_PORT))
                logger.info(
                    "SENT    periodic update to %s (%d routes)",
                    neighbor,
                    len(packet["routes"]),
                )
            except OSError as exc:
                logger.error("SEND    failed to %s: %s", neighbor, exc)

        # ---- Garbage-collect poisoned routes that have been held long enough ----
        # First, snapshot currently-connected subnets OUTSIDE the lock
        # to avoid holding the lock during a subprocess call.
        current_connected = discover_connected_subnets()

        with table_lock:
            expired = []
            for subnet in list(poison_hold.keys()):
                poison_hold[subnet] -= 1
                if poison_hold[subnet] <= 0:
                    expired.append(subnet)
            for subnet in expired:
                del poison_hold[subnet]
                if subnet in routing_table:
                    dist, _ = routing_table[subnet]
                    if dist >= MAX_DISTANCE:
                        # --- CONNECTED-ROUTE PROTECTION ---
                        # If this subnet is currently connected (interface
                        # still present), it was falsely poisoned.  Restore
                        # it to distance 0 instead of flushing.
                        if subnet in current_connected:
                            logger.info(
                                "GC-RESTORE  %s is still connected — restoring to distance 0 "
                                "(was falsely poisoned)",
                                subnet,
                            )
                            routing_table[subnet] = [0, "0.0.0.0"]
                            # Restore the kernel route too
                            dev = current_connected[subnet]
                            try:
                                subprocess.call(
                                    ["ip", "route", "replace", subnet, "dev", dev],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                )
                            except Exception:
                                pass
                        else:
                            logger.info(
                                "FLUSH   %s removed after %d poison-hold cycles",
                                subnet, POISON_HOLD_CYCLES,
                            )
                            remove_route(subnet)
                            del routing_table[subnet]

        # ---- Periodic routing table log (for convergence demo) ----
        print_routing_table("Periodic broadcast snapshot")

        # ---- Convergence detection ----
        # (stable_cycles is reset to 0 in update_logic when a change occurs)
        with table_lock:
            stable_cycles += 1
            cycles = stable_cycles
            announced = converged_announced

        if cycles >= CONVERGE_CYCLES and not announced:
            logger.info(
                "════════════════════════════════════════════════════"
            )
            logger.info(
                "  ✔ Routing table CONVERGED (stable for %d cycles)", cycles
            )
            logger.info(
                "════════════════════════════════════════════════════"
            )
            with table_lock:
                converged_announced = True
        elif cycles < CONVERGE_CYCLES:
            logger.info(
                "CONVERGE  not yet — changes detected recently (%d/%d stable cycles)",
                cycles, CONVERGE_CYCLES,
            )

        time.sleep(BROADCAST_INTERVAL)


def update_logic(neighbor_ip: str, routes_from_neighbor: list[dict]) -> None:
    """
    Process an incoming DV update from `neighbor_ip` using the
    Bellman-Ford relaxation step.

    Algorithm
    ---------
    For every route advertised by the neighbor:
        new_distance = advertised_distance + 1   (the cost to reach
                       that neighbor is 1 hop)

        • If we have no route to that subnet → install it.
        • If new_distance < current distance  → update (shorter path).
        • If the *same* next_hop advertises a changed metric → adopt it
          (the neighbor's view of the world may have changed).
        • If new_distance >= MAX_DISTANCE      → mark unreachable and
          remove from kernel FIB.

    Convergence tracking
    --------------------
    If any route changes, `stable_cycles` is reset to 0 so the
    convergence detector knows the network is still settling.

    Parameters
    ----------
    neighbor_ip : str
        IP of the neighbor that sent this update.
    routes_from_neighbor : list of dict
        Each dict has keys "subnet" (str) and "distance" (int).
    """
    global stable_cycles, converged_announced

    changed = False

    with table_lock:
        for route in routes_from_neighbor:
            subnet   = route.get("subnet")
            adv_dist = route.get("distance")

            if subnet is None or adv_dist is None:
                continue  # Malformed entry — skip silently

            # Bellman-Ford: cost via this neighbor
            new_distance = int(adv_dist) + 1

            # Cap at MAX_DISTANCE (count-to-infinity prevention)
            if new_distance >= MAX_DISTANCE:
                new_distance = MAX_DISTANCE

            current = routing_table.get(subnet)

            if current is None:
                # ----- New route (not yet in our table) -----
                if new_distance < MAX_DISTANCE:
                    routing_table[subnet] = [new_distance, neighbor_ip]
                    logger.info(
                        "ADD     %s via %s distance %d",
                        subnet, neighbor_ip, new_distance,
                    )
                    changed = True
                    apply_route(subnet, neighbor_ip)
                # else: unreachable route — no point adding it

            else:
                cur_dist, cur_nh = current

                # Skip directly-connected subnets — they are immutable.
                # A connected route (distance 0, next_hop 0.0.0.0) must
                # NEVER be overridden by a learned route from any neighbor.
                if cur_dist == 0 and cur_nh == "0.0.0.0":
                    continue

                if cur_nh == neighbor_ip:
                    # The SAME neighbor that gave us this route is now
                    # advertising a different metric.  We MUST adopt it
                    # because our path goes through them.
                    if new_distance != cur_dist:
                        if new_distance >= MAX_DISTANCE:
                            # Route has become unreachable via this neighbor.
                            # Mark as poisoned and start the hold timer so
                            # that the poison is broadcast to other neighbors
                            # before the route is garbage-collected.
                            routing_table[subnet] = [MAX_DISTANCE, neighbor_ip]
                            poison_hold[subnet] = POISON_HOLD_CYCLES
                            logger.info(
                                "POISON  %s via %s (metric %d → %d, holding for %d cycles)",
                                subnet, neighbor_ip, cur_dist, new_distance,
                                POISON_HOLD_CYCLES,
                            )
                            remove_route(subnet)
                        else:
                            routing_table[subnet] = [new_distance, neighbor_ip]
                            logger.info(
                                "UPDATE  %s via %s distance %d → %d",
                                subnet, neighbor_ip, cur_dist, new_distance,
                            )
                            apply_route(subnet, neighbor_ip)
                        changed = True

                elif new_distance < cur_dist:
                    # ----- Shorter path found through a different neighbor -----
                    routing_table[subnet] = [new_distance, neighbor_ip]
                    logger.info(
                        "UPDATE  %s via %s distance %d → %d (better path)",
                        subnet, neighbor_ip, cur_dist, new_distance,
                    )
                    changed = True
                    apply_route(subnet, neighbor_ip)
                    # If this subnet was in poison_hold, cancel it — we
                    # found a viable alternative path.
                    poison_hold.pop(subnet, None)

                elif cur_dist >= MAX_DISTANCE and new_distance < MAX_DISTANCE:
                    # ----- Route was poisoned but a DIFFERENT neighbor now
                    #        offers a viable alternative.  Accept it even if
                    #        it's not "shorter" than the poisoned distance,
                    #        because any finite distance is better than ∞. -----
                    routing_table[subnet] = [new_distance, neighbor_ip]
                    logger.info(
                        "RECOVER %s via %s distance %d (was poisoned at %d)",
                        subnet, neighbor_ip, new_distance, cur_dist,
                    )
                    changed = True
                    apply_route(subnet, neighbor_ip)
                    poison_hold.pop(subnet, None)

        # ---- Reset convergence counter if anything changed ----
        if changed:
            stable_cycles = 0
            converged_announced = False

    # ---- Log the result and fire triggered update ----
    if changed:
        print_routing_table("Updated routing table")
        # RFC 2453 §3.10.1 — send an immediate triggered update so
        # that neighbors learn about the change without waiting for
        # the next periodic broadcast.
        trigger_update()
    else:
        logger.info("STABLE  No changes from update by %s (network stable)", neighbor_ip)


# ---------------------------------------------------------------------------
# Neighbor failure detection
# ---------------------------------------------------------------------------

def neighbor_timeout_checker() -> None:
    """
    Periodically check whether any neighbor has gone silent.

    If a neighbor hasn't sent an update within NEIGHBOR_TIMEOUT seconds,
    all routes learned from that neighbor follow a two-phase withdrawal
    that mirrors real RIP "triggered update" behavior:

      Phase 1 — POISON:  Set distance to MAX_DISTANCE (16) and start
                the poison_hold timer.  The poisoned route remains in
                the table so that the next broadcast(s) will advertise
                the unreachability to every peer.

      Phase 2 — FLUSH:   After POISON_HOLD_CYCLES broadcasts the route
                is garbage-collected by the broadcast_updates loop.

    Also monitors physical link status with debounce — a connected
    subnet must be missing from `ip addr` for LINK_DOWN_THRESHOLD
    consecutive checks before it is declared down.  This prevents
    transient kernel/Docker races from falsely poisoning connected
    routes.

    Runs forever in a daemon thread.
    """
    global stable_cycles, converged_announced

    logger.info("TIMEOUT checker started (threshold=%ds)", NEIGHBOR_TIMEOUT)

    while True:
        time.sleep(NEIGHBOR_TIMEOUT / 3)  # Check frequently
        now = time.time()
        dead_neighbors: list[str] = []

        # ---- 1. Check for physical link changes (with debounce) ----
        current_interfaces = discover_connected_subnets()
        changed_links = False

        with table_lock:
            # Check for dead links (subnets that were directly connected
            # but are no longer present in the interface list).
            # Uses debounce: only declare down after LINK_DOWN_THRESHOLD
            # consecutive misses.
            for subnet, (dist, nh) in list(routing_table.items()):
                if nh == "0.0.0.0" and dist < MAX_DISTANCE:
                    if subnet not in current_interfaces:
                        # Increment miss counter
                        link_miss_count[subnet] = link_miss_count.get(subnet, 0) + 1
                        misses = link_miss_count[subnet]

                        if misses >= LINK_DOWN_THRESHOLD:
                            # Confirmed down after multiple consecutive misses
                            logger.warning(
                                "LINK DOWN %s — confirmed after %d consecutive misses, poisoning route",
                                subnet, misses,
                            )
                            routing_table[subnet] = [MAX_DISTANCE, "0.0.0.0"]
                            poison_hold[subnet] = POISON_HOLD_CYCLES
                            remove_route(subnet)
                            changed_links = True
                            link_miss_count.pop(subnet, None)
                        else:
                            logger.info(
                                "LINK MAYBE %s — miss %d/%d (debouncing, not yet confirmed)",
                                subnet, misses, LINK_DOWN_THRESHOLD,
                            )
                    else:
                        # Interface is present — reset miss counter
                        link_miss_count.pop(subnet, None)

            # Check for recovered or new links
            for subnet, dev_name in current_interfaces.items():
                entry = routing_table.get(subnet)
                # If it's missing, or currently poisoned / learned (dist > 0)
                if not entry or entry[0] > 0:
                    logger.info("LINK UP %s on %s — adding connected route", subnet, dev_name)
                    # Explicitly instruct the kernel to associate the subnet
                    # with the hardware interface directly
                    try:
                        subprocess.call(
                            ["ip", "route", "replace", subnet, "dev", dev_name],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    except Exception:
                        pass
                    routing_table[subnet] = [0, "0.0.0.0"]
                    poison_hold.pop(subnet, None)
                    link_miss_count.pop(subnet, None)
                    changed_links = True

            if changed_links:
                stable_cycles = 0
                converged_announced = False

        if changed_links:
            print_routing_table("After physical link status changed")
            trigger_update()

        # ---- 2. Check for neighbor timeouts ----
        with table_lock:
            for neighbor in NEIGHBORS:
                last = neighbor_last_seen.get(neighbor)
                if last is None:
                    # We've never heard from this neighbor yet — don't
                    # penalise it until at least NEIGHBOR_TIMEOUT has
                    # elapsed since our own startup.
                    continue
                if now - last > NEIGHBOR_TIMEOUT:
                    dead_neighbors.append(neighbor)

        # Poison routes learned from dead neighbors
        for neighbor in dead_neighbors:
            logger.warning(
                "TIMEOUT neighbor %s has not responded in %ds — poisoning routes",
                neighbor, NEIGHBOR_TIMEOUT,
            )
            with table_lock:
                poisoned = False
                # Use list() to take a snapshot — we mutate values during iteration
                for subnet, entry in list(routing_table.items()):
                    dist, nh = entry
                    # Only poison routes that were LEARNED through this
                    # neighbor (next_hop == neighbor AND distance > 0).
                    # Directly-connected subnets (distance 0) are never
                    # poisoned — the link is still physically present.
                    if nh == neighbor and 0 < dist < MAX_DISTANCE:
                        # Phase 1: mark as unreachable, start hold timer
                        routing_table[subnet] = [MAX_DISTANCE, neighbor]
                        poison_hold[subnet] = POISON_HOLD_CYCLES
                        logger.info(
                            "POISON  %s via %s (neighbor timeout, holding for %d cycles)",
                            subnet, neighbor, POISON_HOLD_CYCLES,
                        )
                        remove_route(subnet)
                        poisoned = True

                # ALWAYS clear the timestamp to stop repeated TIMEOUT
                # messages.  If the neighbor comes back, it will get a
                # fresh timestamp from its next packet.
                neighbor_last_seen.pop(neighbor, None)

                if poisoned:
                    # Reset convergence since the table changed
                    stable_cycles = 0
                    converged_announced = False
                else:
                    logger.info(
                        "TIMEOUT no learned routes via %s to poison (connected subnets unaffected)",
                        neighbor,
                    )

            if poisoned:
                print_routing_table("After neighbor timeout — routes poisoned (awaiting flush)")
                # Fire a triggered update so neighbors immediately learn
                # about the poisoned routes (don't wait for next periodic).
                trigger_update()


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------

def listen_for_updates() -> None:
    """
    Main-thread loop: binds to UDP port 5000 and processes every
    incoming DV-JSON packet.

    Steps per packet:
      1. Decode JSON
      2. Validate version and structure
      3. **Adjacency check**: verify sender IP is on a directly-connected
         subnet.  This is CRITICAL — Docker bridge networking can deliver
         packets across networks via gateway routing (e.g. after a link
         detach), causing the sender IP to be unreachable as a next-hop.
         Accepting such packets would install kernel routes that fail.
      4. Validate router_id vs sender IP (log mismatches)
      5. Record the neighbor's liveness timestamp
      6. Delegate to update_logic()
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", UDP_PORT))

    logger.info("LISTEN  UDP port %d on %s", UDP_PORT, MY_IP)

    while True:
        try:
            data, addr = sock.recvfrom(BUFFER_SIZE)
            sender_ip = addr[0]

            # Parse the DV-JSON packet
            try:
                packet = json.loads(data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning("RECV    malformed packet from %s: %s", sender_ip, exc)
                continue

            # Basic validation
            version = packet.get("version")
            router_id = packet.get("router_id")
            routes = packet.get("routes")

            # Version check — accept both integer (1) and float (1.0)
            # to handle cross-implementation compatibility.
            if version is None or int(version) != DV_VERSION:
                logger.warning(
                    "RECV    version mismatch from %s (got %s, expected %s)",
                    sender_ip, version, DV_VERSION,
                )
                continue

            if not isinstance(routes, list):
                logger.warning("RECV    invalid 'routes' field from %s", sender_ip)
                continue

            # ---- ADJACENCY CHECK (critical for correctness) ----
            # Verify the sender's IP is on one of our directly-connected
            # subnets.  If not, this packet arrived via Docker gateway
            # cross-network routing — the sender is NOT a true neighbor
            # and using their IP as next_hop would create broken kernel
            # routes.  Drop the packet silently.
            if not is_on_my_subnet(sender_ip):
                logger.warning(
                    "RECV    DROPPED packet from %s — not on any connected subnet "
                    "(likely Docker cross-network gateway leak)",
                    sender_ip,
                )
                continue

            # ---- router_id validation ----
            # For multi-homed routers (e.g. Router B on two networks),
            # the router_id may differ from the sender IP because the
            # router auto-detected one of its several IPs as its ID.
            # This is normal and expected — we log it for debugging but
            # still process the update using the actual sender IP.
            if router_id and router_id != sender_ip:
                logger.info(
                    "RECV    router_id note: packet says '%s' but sender is '%s' (multi-homed)",
                    router_id, sender_ip,
                )

            logger.info(
                "RECV    update from %s (router_id=%s, %d routes)",
                sender_ip, router_id, len(routes),
            )

            # Record liveness — use the sending IP, not the router_id
            with table_lock:
                neighbor_last_seen[sender_ip] = time.time()

            # Run the Bellman-Ford update
            update_logic(sender_ip, routes)

        except OSError as exc:
            logger.error("LISTEN  socket error: %s", exc)
            time.sleep(1)  # Back off before retrying


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Bootstrap the Distance-Vector Routing Daemon.

    Startup sequence:
      1. Discover connected subnets → seed routing table (distance 0).
      2. Launch the periodic broadcast thread.
      3. Launch the neighbor-timeout checker thread.
      4. Enter the main listening loop.
    """
    logger.info("═" * 60)
    logger.info("  Distance-Vector Routing Daemon  (Bellman-Ford + Poison Reverse)")
    logger.info("  Router IP       : %s", MY_IP)
    logger.info("  Neighbors       : %s", ", ".join(NEIGHBORS) if NEIGHBORS else "(none)")
    logger.info("  Port            : %d", UDP_PORT)
    logger.info("  Max metric      : %d", MAX_DISTANCE)
    logger.info("  Broadcast       : every %ds", BROADCAST_INTERVAL)
    logger.info("  Neighbor timeout: %ds", NEIGHBOR_TIMEOUT)
    logger.info("  Poison hold     : %d cycles", POISON_HOLD_CYCLES)
    logger.info("  Converge after  : %d stable cycles", CONVERGE_CYCLES)
    logger.info("  Link-down thresh: %d consecutive misses", LINK_DOWN_THRESHOLD)
    logger.info("═" * 60)

    # Step 0 — create shared send socket (reused by broadcast + trigger)
    global _send_sock
    _send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    logger.info("SOCKET  shared UDP send socket created")

    # Step 1 — seed routing table
    initialize_routing_table()

    # Step 2 — periodic broadcaster (daemon thread)
    broadcast_thread = threading.Thread(
        target=broadcast_updates,
        daemon=True,
        name="dv-broadcast",
    )
    broadcast_thread.start()

    # Step 3 — neighbor timeout checker (daemon thread)
    timeout_thread = threading.Thread(
        target=neighbor_timeout_checker,
        daemon=True,
        name="dv-timeout",
    )
    timeout_thread.start()

    # Step 4 — listen on main thread (blocking)
    listen_for_updates()


if __name__ == "__main__":
    main()
