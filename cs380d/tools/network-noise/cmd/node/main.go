// noise-node: distributed application node that uses etcd for metadata consensus.
//
// PURPOSE
// -------
// This binary emulates a "real" distributed application (e.g. a distributed
// cache, object store, or analytics pipeline) that:
//   1. Accepts client writes over HTTP
//   2. Replicates each write to peer nodes via UDP — this is the NIC-saturation
//      source, independent of Raft
//   3. Periodically flushes metadata (op counters, health) to etcd via etcdctl
//      at a CONTROLLED, LOW rate (every `batch` ops)
//
// WHY THIS TRIGGERS ELECTIONS
// ---------------------------
// In etcd Raft, ANY message from the leader (AppendEntries carrying log entries
// OR empty heartbeats) resets the follower election timer.  Under a direct write
// flood, AppendEntries are constant, so timers never expire.
//
// Here, the UDP peer traffic saturates the tc-tbf-capped loopback.  etcd's
// TCP segments (both AppendEntries and heartbeats) queue behind UDP packets.
// When queuing delay > election_timeout, elections fire.  The etcd write rate
// is intentionally low so AppendEntries are rare, giving the election timer
// room to expire.
//
// USAGE (3 nodes on localhost)
//   noise-node -id node1 -listen :19001 -repl :18001 \
//              -peers 127.0.0.1:18002,127.0.0.1:18003 \
//              -etcdctl /path/to/etcdctl \
//              -etcd http://127.0.0.1:2379,http://127.0.0.1:22379,http://127.0.0.1:32379 \
//              -batch 2000
package main

import (
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"sync/atomic"
	"syscall"
	"time"
)

// etcdPut writes key=value to the etcd cluster using etcdctl subprocess.
// Runs asynchronously; failures are logged but do NOT fail the caller.
// Using etcdctl avoids any gRPC-gateway dependency and works with any etcd version.
func etcdPutAsync(etcdctlPath, endpoints, key, value string) {
	go func() {
		cmd := exec.Command(etcdctlPath,
			"--endpoints="+endpoints,
			"put", key, value,
		)
		// Discard stdout/stderr; we don't care about the output, only the side
		// effect (writing a Raft proposal).  Failures during elections are normal.
		cmd.Stdout = io.Discard
		cmd.Stderr = io.Discard
		_ = cmd.Run()
	}()
}

// node holds all state for a single noise-node instance.
type node struct {
	id       string
	peers    []*net.UDPConn // pre-connected UDP sockets to peer repl ports
	pktSize  int

	// etcd metadata config
	etcdctl   string
	etcdEPs   string // comma-separated, passed directly to etcdctl --endpoints
	batchSize int64  // write to etcd every N ops (0 = never)

	// Counters (accessed atomically)
	totalOps  int64
	totalErrs int64
	batchAcc  int64 // ops since last etcd flush
}

func (n *node) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/write":
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		n.handleWrite(w, r)
	case "/health":
		w.WriteHeader(http.StatusOK)
		fmt.Fprintf(w, `{"node":%q,"ops":%d}`, n.id, atomic.LoadInt64(&n.totalOps))
	default:
		http.NotFound(w, r)
	}
}

func (n *node) handleWrite(w http.ResponseWriter, r *http.Request) {
	// Read body (client payload).  We don't store it — just use it to size
	// the UDP replication packet.
	body, err := io.ReadAll(io.LimitReader(r.Body, 16*1024*1024))
	r.Body.Close()
	if err != nil {
		atomic.AddInt64(&n.totalErrs, 1)
		http.Error(w, "read body: "+err.Error(), http.StatusInternalServerError)
		return
	}

	// Build replication packet: fill to pktSize with payload data.
	pkt := make([]byte, n.pktSize)
	copy(pkt, body)

	// ── Peer UDP replication ─────────────────────────────────────────────────
	// This is the NIC-saturation mechanism.  Each write fans out to all peers.
	// At 5000 writes/s with 4KB bodies → 2 peers → 40 MB/s inter-node UDP,
	// which saturates a 200 Mbit/s tc-tbf cap.
	for _, conn := range n.peers {
		_, _ = conn.Write(pkt)
	}

	ops := atomic.AddInt64(&n.totalOps, 1)

	// ── Metadata flush to etcd ───────────────────────────────────────────────
	// Only every batchSize ops.  At batch=2000, 5000 ops/s → 2.5 etcd
	// writes/s per node → ~7.5 total → AppendEntries every ~130ms.
	// election_timeout=300ms means 2 consecutive misses trigger an election.
	if n.batchSize > 0 {
		acc := atomic.AddInt64(&n.batchAcc, 1)
		if acc >= n.batchSize {
			// CAS-style: subtract batch size and only proceed if we "won" the flush
			if atomic.CompareAndSwapInt64(&n.batchAcc, acc, acc-n.batchSize) {
				key := fmt.Sprintf("/noise/ops/%s", n.id)
				val := fmt.Sprintf("%d", ops)
				etcdPutAsync(n.etcdctl, n.etcdEPs, key, val)
			}
		}
	}

	w.WriteHeader(http.StatusOK)
}

func main() {
	idFlag       := flag.String("id",       "",                                          "Node ID (default: listen address)")
	listenFlag   := flag.String("listen",   ":19001",                                    "HTTP listen address for client requests")
	replFlag     := flag.String("repl",     ":18001",                                    "UDP listen port to absorb peer replication")
	peersFlag    := flag.String("peers",    "",                                          "Comma-separated peer UDP replication addresses (e.g., 127.0.0.1:18002,127.0.0.1:18003)")
	etcdctlFlag  := flag.String("etcdctl",  "etcdctl",                                   "Path to etcdctl binary")
	etcdFlag     := flag.String("etcd",     "http://127.0.0.1:2379",                     "Comma-separated etcd HTTP endpoints (passed to etcdctl)")
	batchFlag    := flag.Int64("batch",     2000,                                        "Write one key to etcd every N client ops (0=never)")
	pktFlag      := flag.Int("pkt",         1400,                                        "UDP replication packet size in bytes")
	flag.Parse()

	id := *idFlag
	if id == "" {
		id = *listenFlag
	}

	// ── UDP sink: absorb replication packets from peers ───────────────────────
	sinkAddr, err := net.ResolveUDPAddr("udp4", *replFlag)
	if err != nil {
		fmt.Fprintf(os.Stderr, "resolve repl addr %s: %v\n", *replFlag, err)
		os.Exit(1)
	}
	sink, err := net.ListenUDP("udp4", sinkAddr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "UDP listen %s: %v\n", *replFlag, err)
		os.Exit(1)
	}
	_ = sink.SetReadBuffer(16 * 1024 * 1024)
	go func() {
		buf := make([]byte, 65536)
		for {
			_, _, err := sink.ReadFromUDP(buf)
			if err != nil {
				return
			}
		}
	}()

	// ── Connect to peer UDP ports ─────────────────────────────────────────────
	var peerConns []*net.UDPConn
	if *peersFlag != "" {
		for _, p := range strings.Split(*peersFlag, ",") {
			p = strings.TrimSpace(p)
			if p == "" {
				continue
			}
			addr, err := net.ResolveUDPAddr("udp4", p)
			if err != nil {
				fmt.Fprintf(os.Stderr, "bad peer %s: %v\n", p, err)
				continue
			}
			conn, err := net.DialUDP("udp4", nil, addr)
			if err != nil {
				fmt.Fprintf(os.Stderr, "dial peer %s: %v\n", p, err)
				continue
			}
			_ = conn.SetWriteBuffer(4 * 1024 * 1024)
			peerConns = append(peerConns, conn)
		}
	}

	n := &node{
		id:        id,
		peers:     peerConns,
		pktSize:   *pktFlag,
		etcdctl:   *etcdctlFlag,
		etcdEPs:   *etcdFlag,
		batchSize: *batchFlag,
	}

	fmt.Printf("[node %s] listen=%s  repl=%s  peers=%d  batch=%d  etcdctl=%s\n",
		id, *listenFlag, *replFlag, len(peerConns), *batchFlag, *etcdctlFlag)

	// Register this node in etcd so clients can discover it
	etcdPutAsync(*etcdctlFlag, *etcdFlag,
		"/noise/nodes/"+id,
		fmt.Sprintf(`{"listen":%q,"repl":%q}`, *listenFlag, *replFlag),
	)

	// Periodic heartbeat: proves to etcd watchers the node is alive.
	// Runs every 5 seconds — produces ~0.6 etcd writes/second for 3 nodes,
	// which is the floor for AppendEntries rate.
	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			hb := fmt.Sprintf(`{"ops":%d,"ts":%d}`,
				atomic.LoadInt64(&n.totalOps), time.Now().Unix())
			etcdPutAsync(*etcdctlFlag, *etcdFlag, "/noise/heartbeat/"+id, hb)
		}
	}()

	// Stats printer
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		prev := int64(0)
		for range ticker.C {
			cur  := atomic.LoadInt64(&n.totalOps)
			errs := atomic.LoadInt64(&n.totalErrs)
			bsz  := n.batchSize
			etcdRate := int64(0)
			if bsz > 0 {
				etcdRate = (cur - prev) / bsz
			}
			fmt.Printf("[node %s] ops/s=%d  etcd_writes/s≈%d  total=%d  errs=%d\n",
				id, cur-prev, etcdRate, cur, errs)
			prev = cur
		}
	}()

	// Graceful shutdown
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	srv := &http.Server{
		Addr:    *listenFlag,
		Handler: n,
	}
	go func() {
		<-stop
		// Unregister from etcd on clean shutdown
		etcdPutAsync(*etcdctlFlag, *etcdFlag, "/noise/nodes/"+id, `{"status":"down"}`)
		time.Sleep(100 * time.Millisecond)
		srv.Close()
	}()

	fmt.Printf("[node %s] HTTP ready on %s\n", id, *listenFlag)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fmt.Fprintf(os.Stderr, "serve: %v\n", err)
		os.Exit(1)
	}
}
