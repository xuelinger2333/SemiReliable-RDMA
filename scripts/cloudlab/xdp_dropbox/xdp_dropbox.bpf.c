// SPDX-License-Identifier: GPL-2.0
//
// xdp_dropbox — XDP/eBPF RoCE v2 loss-injection forwarder.
//
// Model
// -----
// One amd-class node (amd186) sits on the same L2 subnet as the two training
// nodes (amd196 ↔ amd203).  We force all amd196↔amd203 RoCE traffic to go
// through amd186 by ARP-spoofing the peers (see arp_spoof_setup.sh): each
// training node's ARP table maps the other's IP to amd186's MAC, so packets
// physically land on amd186's NIC.
//
// This XDP program, attached to amd186's experiment-LAN port in DRV mode,
// makes three decisions for every inbound packet:
//
//   1. If it's NOT IPv4+UDP:4791 (RoCE v2), return XDP_PASS so the kernel
//      handles it normally (ARP replies, ICMP, SSH on mgmt-NIC traffic that
//      somehow leaked in — all must keep working).
//
//   2. If it IS RoCE v2, roll bpf_get_prandom_u32() % 1_000_000 against the
//      user-provided drop rate (stored in `drop_rate_map`, units = ppm).
//      If the roll < rate → XDP_DROP.  Clean Bernoulli, no state.
//
//   3. If we keep the packet, rewrite the Ethernet dst_mac based on ip.daddr
//      → peer_macs lookup.  amd196's IP maps to amd196's real MAC, amd203's
//      IP maps to amd203's real MAC.  Then XDP_TX sends it back out the
//      same port — the NIC hardware won't alter src_mac, so the receiver
//      sees the packet as if it came from the original sender directly.
//
// This is a "bump in the wire" at L2, with the minimum bytes changed (6 —
// just dst_mac).  RoCE v2 BTH / RETH / etc. are untouched because RoCE
// identifies endpoints by QPN, not MAC.
//
// Performance
// -----------
// XDP_DRV on mlx5 sustains ≥20 Mpps per core (Cilium / Katran benchmarks).
// Training's AllReduce offered rate is ~30 Kpps.  uc_blaster calibration at
// 15 Gbps offered is ~1.5 Mpps.  Line rate on 25 GbE = 37 Mpps for 64 B
// packets, ~2 Mpps for 1500 B.  All within single-core XDP capacity.
//
// Maps
// ----
//   peer_macs       hash{__be32 ip → __u8[6] mac}       populated at load
//   drop_rate_map   array[1] = __u32 rate_ppm           live-tweakable
//   stats_map       percpu array[4] = __u64 counters    rx/roce/dropped/tx
//
// Counters are exposed as `bpftool map dump pinned /sys/fs/bpf/xdp_dropbox/
// stats_map` — middlebox_setup.sh status reads and sums across CPUs.

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/udp.h>
#include <linux/in.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

// RoCE v2 destination UDP port.  Standard, hard-coded.
#define ROCE_V2_UDP_PORT 4791

// Stats map indices.
#define STAT_RX_TOTAL  0
#define STAT_RX_ROCE   1
#define STAT_DROPPED   2
#define STAT_TX_OK     3
#define STAT_COUNT     4

// ---------------------------------------------------------------------------
// BPF maps
// ---------------------------------------------------------------------------

// IPv4 daddr (__be32) → 6-byte Ethernet MAC.  Populated by the loader
// (middlebox_setup.sh start) based on /etc/hosts-like peer table.
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 16);
    __type(key, __be32);
    __type(value, unsigned char[6]);
    __uint(pinning, LIBBPF_PIN_BY_NAME);
} peer_macs SEC(".maps");

// drop_rate_map[0] = drop probability in ppm (0..1_000_000).
// Live-tweaked between matrix cells via bpftool map update.
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u32);
    __uint(pinning, LIBBPF_PIN_BY_NAME);
} drop_rate_map SEC(".maps");

// self_mac[0] = the 6-byte MAC of the interface XDP is attached to.  We
// rewrite src_mac on every XDP_TX so the upstream switch's MAC-learning
// table stays consistent (no flapping between real-peer MACs seen from
// multiple switch ports) — without this, RoCE traffic stalls because the
// switch treats our forwarding as a MAC-move event and starts dropping.
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, unsigned char[6]);
    __uint(pinning, LIBBPF_PIN_BY_NAME);
} self_mac SEC(".maps");

// Per-CPU counters.  Loader sums across CPUs for reporting.
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, STAT_COUNT);
    __type(key, __u32);
    __type(value, __u64);
    __uint(pinning, LIBBPF_PIN_BY_NAME);
} stats_map SEC(".maps");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static __always_inline void stat_inc(__u32 key)
{
    __u64 *val = bpf_map_lookup_elem(&stats_map, &key);
    if (val)
        (*val)++;
}

// ---------------------------------------------------------------------------
// XDP entry point
// ---------------------------------------------------------------------------

SEC("xdp")
int xdp_dropbox(struct xdp_md *ctx)
{
    void *data_end = (void *)(long)ctx->data_end;
    void *data = (void *)(long)ctx->data;

    stat_inc(STAT_RX_TOTAL);

    // ---- Parse Ethernet ----
    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end)
        return XDP_PASS;
    if (eth->h_proto != bpf_htons(ETH_P_IP))
        return XDP_PASS;    // IPv6, ARP, etc. — let kernel handle normally

    // ---- Parse IPv4 ----
    struct iphdr *ip = (struct iphdr *)(eth + 1);
    if ((void *)(ip + 1) > data_end)
        return XDP_PASS;

    // If dst IP isn't one of the two training peers, the packet is actually
    // addressed to the middlebox (or beyond) — hand it to the kernel so SSH,
    // apt, ICMP to ourselves, ARP probes, etc. keep working.
    unsigned char *new_mac = bpf_map_lookup_elem(&peer_macs, &ip->daddr);
    if (!new_mac)
        return XDP_PASS;

    // From here on, we *know* the packet is meant for a training peer via
    // the ARP-spoof trick.  All such traffic (TCP bootstrap, ICMP, other
    // UDP, and RoCE v2 data) must be transparently forwarded at L2, with
    // only UDP:4791 subject to the Bernoulli drop.

    // ---- Optional: Bernoulli drop on RoCE v2 only ----
    // Parse UDP lazily, and only if the IP header is a common 20-byte
    // form — RoCE v2 packets never use IP options.  If the header shape
    // is anything else (ihl>5, TCP, ICMP, etc.), just forward it.
    int is_roce = 0;
    if (ip->ihl == 5 && ip->protocol == IPPROTO_UDP) {
        struct udphdr *udp = (struct udphdr *)((void *)ip + 20);
        if ((void *)(udp + 1) <= data_end &&
            udp->dest == bpf_htons(ROCE_V2_UDP_PORT)) {
            is_roce = 1;
        }
    }
    if (is_roce) {
        stat_inc(STAT_RX_ROCE);
        __u32 zero = 0;
        __u32 *rate_ppm = bpf_map_lookup_elem(&drop_rate_map, &zero);
        if (rate_ppm && *rate_ppm > 0) {
            __u32 roll = bpf_get_prandom_u32() % 1000000U;
            if (roll < *rate_ppm) {
                stat_inc(STAT_DROPPED);
                return XDP_DROP;
            }
        }
    }

    // ---- Rewrite dst + src MAC and XDP_TX out the same port ----
    // dst_mac: peer's real MAC (so peer's kernel/NIC accepts as local).
    // src_mac: our own MAC (so switch MAC-learning stays consistent and
    //          doesn't treat us as a MAC-flap / port-security violation).
    eth->h_dest[0] = new_mac[0];
    eth->h_dest[1] = new_mac[1];
    eth->h_dest[2] = new_mac[2];
    eth->h_dest[3] = new_mac[3];
    eth->h_dest[4] = new_mac[4];
    eth->h_dest[5] = new_mac[5];

    __u32 zero2 = 0;
    unsigned char *self = bpf_map_lookup_elem(&self_mac, &zero2);
    if (self) {
        eth->h_source[0] = self[0];
        eth->h_source[1] = self[1];
        eth->h_source[2] = self[2];
        eth->h_source[3] = self[3];
        eth->h_source[4] = self[4];
        eth->h_source[5] = self[5];
    }

    stat_inc(STAT_TX_OK);
    return XDP_TX;
}

char LICENSE[] SEC("license") = "GPL";
