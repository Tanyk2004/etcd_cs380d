// really small tc program
//
// userspace marks the etcd stream socket with RAFT_HB_MARK.
// when the controller decides heartbeats need help, it writes 1 into
// boost_config[0]. then this program bumps those packets to a higher priority.

#include <linux/bpf.h>
#include <linux/pkt_cls.h>
#include <bpf/bpf_helpers.h>

#define RAFT_HB_MARK 0x1337u

// one tiny config map:
// key 0 -> 0 means normal mode, 1 means boost heartbeat packets
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u32);
} boost_config SEC(".maps");

SEC("tc")
int tc_raft_hb_prio(struct __sk_buff *skb)
{
	__u32 key = 0;
	__u32 *boost = bpf_map_lookup_elem(&boost_config, &key);

	// if boost is off (or somehow the lookup failed), leave the packet alone
	if (!boost || !*boost)
		return TC_ACT_OK;

	// only change the packets from the marked raft stream socket
	if (skb->mark != RAFT_HB_MARK)
		return TC_ACT_OK;

	// 7 is the control priority, which lands in the highest band of prio qdisc
	skb->priority = 7;

	return TC_ACT_OK;
}

char LICENSE[] SEC("license") = "GPL";
