"""CloudLab profile: DPDK-loss-injection middlebox between two RoCE training nodes.

Topology
--------

    sender (rank 1)  ──[lan_a]──  middlebox (DPDK)  ──[lan_b]──  receiver (rank 0)
                                        │
                                   (optional)
    hammer (ib_write_bw src) ──[lan_a]──┘

- Two separate experiment LANs force *all* RoCE v2 (UDP:4791) packets to traverse
  the middlebox, so the DPDK forwarder can drop them with a configured Bernoulli
  probability.  This is the only way to inject wire-level loss on RoCE (kernel
  `tc netem` is a no-op because RDMA bypasses the kernel on the endpoints).

- Management LAN (public, CloudLab-assigned) is NOT remapped — SSH, apt, git all
  keep working via the management interface on every node.

- Hammer node is optional: included in the default 4-node profile so the old
  `HAMMER_MODE=rdma` sidecar experiment still works, but the main P1 matrix
  does not use it.

Hardware
--------

Default: ``d6515`` (amd-class, ConnectX-5 25 GbE) on every node.  These are the
same nodes as the current ``chen123-302346.rdma-nic-perf-pg0`` profile, which
gives us the CX-5 baseline we already calibrated against in Phase 3 Stage B.

The middlebox node needs *two* ConnectX-5 ports exposed as distinct experiment
interfaces — ``d6515`` has a dual-port CX-5 so this works out of the box.  If
CloudLab assigns a single-port variant, fall back to the VLAN-trunk variant
(see ``SINGLE_NIC_MIDDLEBOX`` parameter below).

Usage
-----

1. Upload this file to CloudLab as a new profile under your project.
2. Instantiate — CloudLab will give every node a public management hostname and
   two experiment-LAN IPs (10.10.1.x for lan_a, 10.10.2.x for lan_b).
3. On every node, clone the SemiRDMA repo and run bootstrap_fresh_node.sh.
4. On the middlebox only, additionally run middlebox_setup.sh bootstrap to
   install DPDK, configure hugepages, and build the dpdk_dropbox forwarder.
5. From the receiver node, launch run_p1_matrix.sh with
   MIDDLEBOX_HOST=<middlebox_mgmt_fqdn> and the desired DROP_RATES.

Profile parameters (editable on the CloudLab "Instantiate" page)
----------------------------------------------------------------

- NODE_TYPE: hardware class for all nodes (default d6515).
- INCLUDE_HAMMER: whether to reserve a 4th node for ib_write_bw hammer
  experiments (default True — keeps sidecar capability).
- SINGLE_NIC_MIDDLEBOX: if True, middlebox gets one CX-5 port and two tagged
  VLANs over it (for profiles where dual-port nodes are scarce).  Default
  False — use this only when the dual-port variant is unavailable.
"""

# geni-lib is only available inside the CloudLab portal sandbox; local linters
# will flag these imports.  This is expected — the file runs in the CloudLab
# portal, not in our repo's venv.
import geni.portal as portal  # noqa: F401  (used via decorators in portal runtime)
import geni.rspec.pg as pg

# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------

pc = portal.Context()

pc.defineParameter(
    "NODE_TYPE",
    "Hardware type for every node (d6515 = CX-5 dual-port amd-class).",
    portal.ParameterType.NODETYPE,
    "d6515",
)
pc.defineParameter(
    "INCLUDE_HAMMER",
    "Include optional 4th node (ib_write_bw hammer source) on lan_a.",
    portal.ParameterType.BOOLEAN,
    True,
)
pc.defineParameter(
    "SINGLE_NIC_MIDDLEBOX",
    "Use 1 NIC + 2 VLAN trunks on middlebox instead of 2 physical ports.",
    portal.ParameterType.BOOLEAN,
    False,
)

params = pc.bindParameters()
request = pc.makeRequestRSpec()

# --------------------------------------------------------------------------
# Standard Ubuntu 22.04 image.  bootstrap_fresh_node.sh installs everything
# else on first boot.
# --------------------------------------------------------------------------
DISK_IMAGE = "urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU22-64-STD"


def _new_node(name: str) -> pg.RawPC:
    node = request.RawPC(name)
    node.hardware_type = params.NODE_TYPE
    node.disk_image = DISK_IMAGE
    return node


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------
sender = _new_node("sender")       # rank 1, trains with SemiRDMA, sends RoCE→middlebox
middlebox = _new_node("middlebox") # DPDK l2fwd + Bernoulli drop on UDP:4791
receiver = _new_node("receiver")   # rank 0, receives RoCE, drives matrix runner

# Optional fourth node — keeps today's hammer sidecar working.
hammer = None
if params.INCLUDE_HAMMER:
    hammer = _new_node("hammer")

# --------------------------------------------------------------------------
# Experiment LANs
#
# lan_a  : sender ↔ middlebox (plus hammer if enabled)
# lan_b  : middlebox ↔ receiver
#
# Both LANs are on the same physical CloudLab switch; VLAN separation ensures
# middlebox is the only L2 path between the two halves.
# --------------------------------------------------------------------------
lan_a = request.LAN("lan_a")
lan_a.bandwidth = 25000000  # 25 Gbps, kbps
lan_a.addInterface(sender.addInterface("if_exp_a"))

lan_b = request.LAN("lan_b")
lan_b.bandwidth = 25000000
lan_b.addInterface(receiver.addInterface("if_exp_b"))

if params.SINGLE_NIC_MIDDLEBOX:
    # Single physical NIC, two VLAN-tagged interfaces on it.  Both lan_a and
    # lan_b attach to the *same* physical port through VLAN demuxing.
    # NOTE: DPDK on a VLAN-tagged single port requires flow-director rules to
    # split rx rings by VLAN.  dpdk_dropbox.c handles this via rte_flow.
    iface = middlebox.addInterface("if_exp_mx")
    lan_a.addInterface(iface)
    lan_b.addInterface(iface)
else:
    # Dual-port CX-5: one port per LAN, cleanest for DPDK l2fwd (one port per
    # rx/tx queue pair, no VLAN classification needed).
    lan_a.addInterface(middlebox.addInterface("if_exp_a"))
    lan_b.addInterface(middlebox.addInterface("if_exp_b"))

if hammer is not None:
    lan_a.addInterface(hammer.addInterface("if_exp_a"))

# --------------------------------------------------------------------------
# Emit rspec
# --------------------------------------------------------------------------
pc.printRequestRSpec(request)
