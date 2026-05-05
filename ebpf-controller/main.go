// ebpf-controller: watches tc qdisc congestion and toggles Raft heartbeat
// priority boosting via a BPF map.
//
// Run as root (needs CAP_NET_ADMIN + CAP_BPF):
//   sudo ./ebpf-controller --iface lo --obj ../bpf/tc_prio.bpf.o
//
// It loads the compiled BPF program, attaches it to the TC egress hook of
// the given interface, then polls the tbf qdisc overlimits counter on that
// same interface.  When the tbf qdisc is actively rate-limiting packets
// (overlimits counter increasing), it writes 1 to the boost_config BPF map
// which tells the kernel program to start prioritizing marked packets.  Once
// the link is no longer congested it turns boosting back off.
//
// Using tc overlimits instead of etcd RTT metrics because the RTT histogram
// only updates every ~30 s (peer probe interval), causing the boost to cycle
// ON for 1 s / OFF for 30 s.  The tc overlimits counter updates every poll.
package main

import (
	"bufio"
	"context"
	"flag"
	"fmt"
	"log"
	"net"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/link"
)

// matches the map/program names in tc_prio.bpf.c
type bpfObjects struct {
	TcRaftHbPrio *ebpf.Program `ebpf:"tc_raft_hb_prio"`
	BoostConfig  *ebpf.Map     `ebpf:"boost_config"`
}

func main() {
	iface      := flag.String("iface", "eth0", "network interface to attach BPF to and watch tc stats on")
	objPath    := flag.String("obj", "../bpf/tc_prio.bpf.o", "path to compiled BPF object")
	interval   := flag.Duration("interval", 200*time.Millisecond, "polling interval")
	hysteresis := flag.Int("hysteresis", 15, "consecutive idle polls before turning boost off (default 15 = 3s at 200ms poll)")
	flag.Parse()

	// load the BPF program from the compiled object file
	spec, err := ebpf.LoadCollectionSpec(*objPath)
	if err != nil {
		log.Fatalf("failed to load BPF spec from %s: %v", *objPath, err)
	}

	var objs bpfObjects
	if err := spec.LoadAndAssign(&objs, nil); err != nil {
		log.Fatalf("failed to load BPF objects: %v", err)
	}
	defer objs.TcRaftHbPrio.Close()
	defer objs.BoostConfig.Close()

	// attach to TC egress on the given interface using TCX (kernel >= 6.6)
	netIface, err := net.InterfaceByName(*iface)
	if err != nil {
		log.Fatalf("interface %q not found: %v", *iface, err)
	}

	tcxLink, err := link.AttachTCX(link.TCXOptions{
		Interface: netIface.Index,
		Program:   objs.TcRaftHbPrio,
		Attach:    ebpf.AttachTCXEgress,
	})
	if err != nil {
		log.Fatalf("failed to attach TC BPF to %s: %v", *iface, err)
	}
	defer tcxLink.Close()
	log.Printf("attached to %s egress", *iface)

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	var (
		prevOverlimits uint64
		cleanCount     int
		boosting       bool
	)

	log.Printf("watching tc tbf overlimits+backlog on %s (hysteresis=%d)", *iface, *hysteresis)

	ticker := time.NewTicker(*interval)
	defer ticker.Stop()

	setBoost := func(enable bool) {
		if enable == boosting {
			return
		}
		val := uint32(0)
		if enable {
			val = 1
		}
		if err := objs.BoostConfig.Put(uint32(0), val); err != nil {
			log.Printf("failed to update boost_config map: %v", err)
			return
		}
		boosting = enable
		if enable {
			log.Printf("boosting ON  - heartbeat packets get priority")
		} else {
			log.Printf("boosting OFF - back to normal scheduling")
		}
	}

	for {
		select {
		case <-ctx.Done():
			setBoost(false)
			log.Println("shutting down")
			return

		case <-ticker.C:
			overlimits, backlog, err := readTbfStats(*iface)
			if err != nil {
				log.Printf("tc stat failed: %v", err)
				continue
			}

			delta := overlimits - prevOverlimits
			prevOverlimits = overlimits

			// Congested when:
			//   delta > 0  — new packets were rate-limited in this 200ms window
			//   backlog > 0 — packets are currently queued in the tbf (link is full)
			// Using both signals prevents false "idle" reads during bursty floods
			// where a 200ms window might have zero new overlimits even though the
			// link remains saturated.
			congested := delta > 0 || backlog > 0

			if congested {
				cleanCount = 0
				setBoost(true)
			} else if boosting {
				cleanCount++
				if cleanCount >= *hysteresis {
					setBoost(false)
					cleanCount = 0
				}
			}
		}
	}
}

// readTbfStats runs `tc -s qdisc show dev <iface>` and returns the cumulative
// overlimits counter and current backlog_bytes from the tbf qdisc.
func readTbfStats(iface string) (overlimits, backlogBytes uint64, err error) {
	out, e := exec.Command("tc", "-s", "qdisc", "show", "dev", iface).Output()
	if e != nil {
		return 0, 0, fmt.Errorf("tc -s qdisc show dev %s: %w", iface, e)
	}
	overlimits, backlogBytes = parseTbfStats(string(out))
	return
}

// parseTbfStats finds the tbf qdisc block in `tc -s qdisc show` output and
// extracts the cumulative overlimits count and current backlog in bytes.
//
// Example output block:
//   qdisc tbf 20: parent 1:2 rate 100Mbit burst 250Kb lat 200.0ms
//    Sent 123456 bytes 789 pkt (dropped 0, overlimits 4567 requeues 0)
//    backlog 12345b 10p requeues 0
func parseTbfStats(output string) (overlimits, backlogBytes uint64) {
	inTbf := false
	scanner := bufio.NewScanner(strings.NewReader(output))
	for scanner.Scan() {
		trimmed := strings.TrimSpace(scanner.Text())

		if strings.HasPrefix(trimmed, "qdisc tbf") {
			inTbf = true
			continue
		}
		if !inTbf {
			continue
		}
		// new qdisc block — tbf section is done
		if strings.HasPrefix(trimmed, "qdisc") {
			break
		}
		// "Sent ... (dropped N, overlimits N requeues N)"
		if strings.Contains(trimmed, "overlimits") {
			idx := strings.Index(trimmed, "overlimits")
			after := strings.Fields(trimmed[idx:])
			if len(after) >= 2 {
				v, err := strconv.ParseUint(after[1], 10, 64)
				if err == nil {
					overlimits = v
				}
			}
		}
		// "backlog Nb Np requeues N"  — N followed by 'b' suffix
		if strings.HasPrefix(trimmed, "backlog") {
			fields := strings.Fields(trimmed)
			if len(fields) >= 2 {
				raw := strings.TrimSuffix(fields[1], "b")
				v, err := strconv.ParseUint(raw, 10, 64)
				if err == nil {
					backlogBytes = v
				}
			}
		}
	}
	return
}
