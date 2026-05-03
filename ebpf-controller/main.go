// ebpf-controller: watches etcd metrics and toggles Raft heartbeat priority
// boosting via a BPF map.
//
// Run as root (needs CAP_NET_ADMIN + CAP_BPF):
//   sudo ./ebpf-controller --iface eth0 --obj ../bpf/tc_prio.bpf.o \
//       --metrics http://localhost:2381/metrics
//
// It loads the compiled BPF program, attaches it to the TC egress hook of
// the given interface, then polls etcd's /metrics endpoint. If heartbeat
// failures start increasing or peer RTT goes above --rtt-ms, it writes 1 to
// the boost_config BPF map which tells the kernel program to start
// prioritizing marked packets. Once things calm down it turns it back off.
package main

import (
	"bufio"
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
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
	iface := flag.String("iface", "eth0", "network interface to attach to")
	objPath := flag.String("obj", "../bpf/tc_prio.bpf.o", "path to compiled BPF object")
	metricsURL := flag.String("metrics", "http://localhost:2381/metrics", "etcd metrics endpoint")
	interval := flag.Duration("interval", 200*time.Millisecond, "polling interval")
	rttThresholdMs := flag.Float64("rtt-ms", 120.0, "mean peer RTT threshold (ms) to trigger boost; set above baseline (~100ms with 50ms netem) so boost only fires during congestion")
	hysteresis := flag.Int("hysteresis", 5, "clean samples needed before turning boost off")
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
		prevRttSum, prevRttCount float64
		cleanCount               int
		boosting                 bool
	)

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

	log.Printf("watching %s (mean rtt threshold=%.1fms, hysteresis=%d)", *metricsURL, *rttThresholdMs, *hysteresis)

	for {
		select {
		case <-ctx.Done():
			setBoost(false)
			log.Println("shutting down")
			return

		case <-ticker.C:
			m, err := scrapeMetrics(*metricsURL)
			if err != nil {
				log.Printf("metrics scrape failed: %v", err)
				continue
			}

			// etcd_network_peer_round_trip_time_seconds is a histogram, not a
			// summary, so there is no quantile="0.99" label.  Compute the mean
			// RTT from the delta of _sum/_count between consecutive samples.
			rttSum   := m["etcd_network_peer_round_trip_time_seconds_sum"]
			rttCount := m["etcd_network_peer_round_trip_time_seconds_count"]
			var rttMeanMs float64
			dCount := rttCount - prevRttCount
			if dCount > 0 {
				rttMeanMs = (rttSum - prevRttSum) / dCount * 1000
			}
			prevRttSum, prevRttCount = rttSum, rttCount

			rttHigh := dCount > 0 && rttMeanMs > *rttThresholdMs

			if rttHigh {
				cleanCount = 0
				setBoost(true)
			} else if boosting && dCount > 0 {
				// Only count down hysteresis when there's fresh data.
				// When dCount==0 (no new RTT measurements since last poll)
				// we don't know if RTT is low — keep boost ON.
				cleanCount++
				if cleanCount >= *hysteresis {
					setBoost(false)
					cleanCount = 0
				}
			}
		}
	}
}

// scrapeMetrics hits the etcd Prometheus endpoint and returns a flat map of
// metric name -> value. For summary quantiles it adds a "_p99" suffix.
func scrapeMetrics(url string) (map[string]float64, error) {
	resp, err := http.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("unexpected status: %s", resp.Status)
	}
	return parsePrometheusText(resp.Body), nil
}

// parsePrometheusText is a quick-and-dirty Prometheus text format parser.
// Good enough for our needs - handles counters, gauges, and summary quantiles.
func parsePrometheusText(r io.Reader) map[string]float64 {
	out := make(map[string]float64)
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "#") || line == "" {
			continue
		}

		parts := strings.Fields(line)
		if len(parts) < 2 {
			continue
		}

		val, err := strconv.ParseFloat(parts[len(parts)-1], 64)
		if err != nil {
			// sometimes there's a timestamp as the last field
			if len(parts) >= 3 {
				val, err = strconv.ParseFloat(parts[len(parts)-2], 64)
				if err != nil {
					continue
				}
			} else {
				continue
			}
		}

		nameAndLabels := parts[0]

		// handle summary quantile lines, e.g.:
		//   etcd_network_peer_round_trip_time_seconds{quantile="0.99"} 0.003
		if strings.Contains(nameAndLabels, `quantile="0.99"`) {
			base := nameAndLabels[:strings.Index(nameAndLabels, "{")]
			out[base+"_p99"] = val
			continue
		}

		// strip label set
		if idx := strings.Index(nameAndLabels, "{"); idx != -1 {
			nameAndLabels = nameAndLabels[:idx]
		}
		// sum across all label combinations (e.g. per-peer counters)
		out[nameAndLabels] += val
	}
	return out
}

