// tc-exporter: Prometheus exporter for tc qdisc statistics.
//
// Runs "tc -s qdisc show dev <iface>" on every /metrics scrape and exposes
// per-qdisc counters.  Designed to surface tbf (token bucket filter) queue
// saturation alongside the etcd Raft metrics already in Prometheus.
//
// Exposed metrics (all labeled with interface= and type=):
//   tc_qdisc_bytes_sent_total      - bytes passed through the qdisc
//   tc_qdisc_packets_sent_total    - packets passed through
//   tc_qdisc_drops_total           - packets dropped (queue overflow → TCP retransmit → election)
//   tc_qdisc_overlimits_total      - packets that hit the rate limit (queued, not dropped)
//   tc_qdisc_requeues_total        - packets requeued
//   tc_qdisc_backlog_bytes         - instantaneous queue depth in bytes
//   tc_qdisc_backlog_packets       - instantaneous queue depth in packets
//
// USAGE
//   tc-exporter -iface lo -port :9105
//
// Add to prometheus.yml:
//   - job_name: "tc"
//     static_configs:
//       - targets: ["127.0.0.1:9105"]
package main

import (
	"bufio"
	"bytes"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

var (
	// qdisc header: "qdisc tbf 1: root refcnt 2 ..."
	reHeader = regexp.MustCompile(`^qdisc\s+(\S+)\s+(\S+):\s+`)

	// "Sent 187432192 bytes 45703 pkt (dropped 12841, overlimits 89234 requeues 0)"
	reSent = regexp.MustCompile(
		`Sent\s+(\d+)\s+bytes\s+(\d+)\s+pkt\s+\(dropped\s+(\d+),\s+overlimits\s+(\d+)\s+requeues\s+(\d+)\)`)

	// "backlog 2490368b 607p requeues 0"
	reBacklog = regexp.MustCompile(`backlog\s+(\d+)b\s+(\d+)p`)
)

type qdiscStats struct {
	qtype   string // "tbf", "netem", "noqueue", …
	handle  string // "1:", "10:", …
	bytesSent   uint64
	pktsSent    uint64
	dropped     uint64
	overlimits  uint64
	requeues    uint64
	backlogBytes uint64
	backlogPkts  uint64
}

func scrapeQdiscs(iface string) ([]qdiscStats, error) {
	out, err := exec.Command("tc", "-s", "qdisc", "show", "dev", iface).Output()
	if err != nil {
		return nil, err
	}

	var result []qdiscStats
	var cur *qdiscStats

	scanner := bufio.NewScanner(bytes.NewReader(out))
	for scanner.Scan() {
		line := scanner.Text()
		trimmed := strings.TrimSpace(line)

		if m := reHeader.FindStringSubmatch(trimmed); m != nil {
			// Start of a new qdisc stanza
			result = append(result, qdiscStats{qtype: m[1], handle: m[2]})
			cur = &result[len(result)-1]
			continue
		}
		if cur == nil {
			continue
		}
		if m := reSent.FindStringSubmatch(trimmed); m != nil {
			cur.bytesSent  = parseU64(m[1])
			cur.pktsSent   = parseU64(m[2])
			cur.dropped    = parseU64(m[3])
			cur.overlimits = parseU64(m[4])
			cur.requeues   = parseU64(m[5])
		}
		if m := reBacklog.FindStringSubmatch(trimmed); m != nil {
			cur.backlogBytes = parseU64(m[1])
			cur.backlogPkts  = parseU64(m[2])
		}
	}
	return result, scanner.Err()
}

func parseU64(s string) uint64 {
	v, _ := strconv.ParseUint(s, 10, 64)
	return v
}

// formatMetrics renders all qdisc stats as Prometheus text format.
func formatMetrics(iface string, qdiscs []qdiscStats) string {
	var b strings.Builder

	writeMeta := func(name, help, mtype string) {
		fmt.Fprintf(&b, "# HELP %s %s\n# TYPE %s %s\n", name, help, name, mtype)
	}

	metrics := []struct {
		name  string
		help  string
		mtype string
		val   func(q qdiscStats) uint64
	}{
		{"tc_qdisc_bytes_sent_total", "Bytes passed through the qdisc", "counter",
			func(q qdiscStats) uint64 { return q.bytesSent }},
		{"tc_qdisc_packets_sent_total", "Packets passed through the qdisc", "counter",
			func(q qdiscStats) uint64 { return q.pktsSent }},
		{"tc_qdisc_drops_total", "Packets dropped due to queue overflow (triggers TCP retransmit)", "counter",
			func(q qdiscStats) uint64 { return q.dropped }},
		{"tc_qdisc_overlimits_total", "Packets that hit the rate limit and were queued (not dropped)", "counter",
			func(q qdiscStats) uint64 { return q.overlimits }},
		{"tc_qdisc_requeues_total", "Packets requeued", "counter",
			func(q qdiscStats) uint64 { return q.requeues }},
		{"tc_qdisc_backlog_bytes", "Instantaneous queue depth in bytes", "gauge",
			func(q qdiscStats) uint64 { return q.backlogBytes }},
		{"tc_qdisc_backlog_packets", "Instantaneous queue depth in packets", "gauge",
			func(q qdiscStats) uint64 { return q.backlogPkts }},
	}

	for _, m := range metrics {
		writeMeta(m.name, m.help, m.mtype)
		for _, q := range qdiscs {
			fmt.Fprintf(&b, `%s{interface=%q,type=%q,handle=%q} %d`+"\n",
				m.name, iface, q.qtype, q.handle, m.val(q))
		}
	}
	return b.String()
}

func main() {
	ifaceFlag := flag.String("iface", "lo", "Network interface to monitor")
	portFlag  := flag.String("port",  ":9105", "Listen address for /metrics")
	flag.Parse()

	iface := *ifaceFlag

	var mu sync.Mutex
	var cached string
	var cacheTime time.Time

	http.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		// Re-scrape at most once per 500ms to avoid hammering tc on concurrent scrapes
		if time.Since(cacheTime) > 500*time.Millisecond {
			qdiscs, err := scrapeQdiscs(iface)
			if err != nil {
				mu.Unlock()
				http.Error(w, "tc scrape failed: "+err.Error(), http.StatusInternalServerError)
				return
			}
			cached = formatMetrics(iface, qdiscs)
			cacheTime = time.Now()
		}
		out := cached
		mu.Unlock()

		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		fmt.Fprint(w, out)
	})

	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `<html><body><a href="/metrics">metrics</a></body></html>`)
	})

	log.Printf("[tc-exporter] Listening on %s, monitoring interface %s", *portFlag, iface)
	if err := http.ListenAndServe(*portFlag, nil); err != nil {
		log.Fatal(err)
	}
}
