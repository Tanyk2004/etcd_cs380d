// noise-node: distributed application node that uses etcd for metadata consensus.
//
// PURPOSE
// -------
// This binary emulates a distributed WAL-journal-based application (e.g. HDFS
// NameNode HA, Ceph MDS, or a distributed transaction coordinator) that:
//   1. Accepts client writes over HTTP
//   2. Replicates each write to peer nodes via UDP — this is the NIC-saturation
//      source, independent of Raft
//   3. Every `batch` ops, flushes a checkpoint to etcd SYNCHRONOUSLY, blocking
//      ALL concurrent handlers until the commit is confirmed
//
// WHY CHECKPOINTS MUST BLOCK ALL HANDLERS
// ----------------------------------------
// The checkpoint is a global durability marker.  Ops accepted after a checkpoint
// window opens but before the marker is committed are "unconfirmed" — a crash
// would lose them.  Therefore the node must stop accepting new work until etcd
// confirms the commit.  This is identical to how WAL journal systems behave:
//   - HDFS NameNode flushes the journal before acknowledging a transaction
//   - Ceph MDS gates new ops on MDS journal flush to the OSD cluster
//   - Kubernetes kubelets cannot be scheduled to without a valid lease renewal
//
// WHY THIS DEMONSTRATES ELECTION COST
// -------------------------------------
// When the etcd cluster is in a leader election, the checkpoint etcdctl call
// blocks for ~election_timeout + stabilization ≈ 240ms.  The WLock held during
// that call pauses all HTTP handlers.  Client-visible QPS drops to near zero for
// the duration, then bursts as queued workers are released.
//
// USAGE (3 nodes on localhost)
//   noise-node -id node1 -listen :19001 -repl :18001 \
//              -peers 127.0.0.1:18002,127.0.0.1:18003 \
//              -etcdctl /path/to/etcdctl \
//              -etcd http://127.0.0.1:2379,http://127.0.0.1:22379,http://127.0.0.1:32379 \
//              -batch 1000
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

// node holds all state for a single noise-node instance.
type node struct {
	id      string
	peers   []*net.UDPConn // pre-connected UDP sockets to peer repl ports
	pktSize int

	// etcd metadata config
	etcdctl   string
	etcdEPs   string // comma-separated, passed directly to etcdctl --endpoints
	batchSize int64  // checkpoint every N ops (0 = never)

	// Blocking checkpoint mutex.
	// handleWrite acquires RLock for the fast path (UDP fan-out + counter).
	// doCheckpoint acquires full Lock, pausing all concurrent handlers until
	// the etcd commit is confirmed.
	renewMu sync.RWMutex

	// Counters (accessed atomically)
	totalOps  int64
	totalErrs int64
	batchAcc  int64 // ops since last checkpoint

	// Stall telemetry (accessed atomically)
	stallCount   int64 // checkpoints that stalled >= minStallMs
	stallMsTotal int64 // cumulative ms spent in doCheckpoint during stalls
	lastStallMs  int64 // duration of the most recent qualifying stall

	minStallMs int64 // config: threshold (ms) to count a checkpoint as a stall
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
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		fmt.Fprintf(w,
			`{"node":%q,"ops":%d,"stall_count":%d,"stall_ms_total":%d,"last_stall_ms":%d}`,
			n.id,
			atomic.LoadInt64(&n.totalOps),
			atomic.LoadInt64(&n.stallCount),
			atomic.LoadInt64(&n.stallMsTotal),
			atomic.LoadInt64(&n.lastStallMs),
		)
	default:
		http.NotFound(w, r)
	}
}

func (n *node) handleWrite(w http.ResponseWriter, r *http.Request) {
	// Acquire read-lock: blocks only when a checkpoint WLock is held.
	// All concurrent handlers proceed in parallel during normal operation.
	n.renewMu.RLock()

	body, err := io.ReadAll(io.LimitReader(r.Body, 16*1024*1024))
	r.Body.Close()
	if err != nil {
		n.renewMu.RUnlock()
		atomic.AddInt64(&n.totalErrs, 1)
		http.Error(w, "read body: "+err.Error(), http.StatusInternalServerError)
		return
	}

	// Build replication packet and fan out to all peers via UDP.
	// This is the NIC-saturation source — independent of Raft.
	pkt := make([]byte, n.pktSize)
	copy(pkt, body)
	for _, conn := range n.peers {
		_, _ = conn.Write(pkt)
	}

	ops := atomic.AddInt64(&n.totalOps, 1)

	// Determine if this goroutine wins the checkpoint CAS.
	// Must happen inside RLock so key/val are captured before unlock.
	var shouldCheckpoint bool
	var ckKey, ckVal string
	if n.batchSize > 0 {
		acc := atomic.AddInt64(&n.batchAcc, 1)
		if acc >= n.batchSize {
			if atomic.CompareAndSwapInt64(&n.batchAcc, acc, acc-n.batchSize) {
				shouldCheckpoint = true
				ckKey = fmt.Sprintf("/noise/ops/%s", n.id)
				ckVal = fmt.Sprintf("%d", ops)
			}
		}
	}

	// Release read-lock BEFORE calling doCheckpoint.
	// doCheckpoint acquires WLock internally; holding RLock here would deadlock.
	n.renewMu.RUnlock()

	// Checkpoint winner blocks here (and blocks all future RLock callers via WLock)
	// until etcd confirms the commit.  The HTTP response to this request is
	// intentionally delayed — it is the "ack after durability" guarantee.
	if shouldCheckpoint {
		n.doCheckpoint(ckKey, ckVal)
	}

	w.WriteHeader(http.StatusOK)
}

// doCheckpoint synchronously commits a progress marker to etcd.
// Holds WLock for the full duration so all concurrent handleWrite calls block
// at their RLock — modeling a WAL journal flush that gates all new operations.
func (n *node) doCheckpoint(key, val string) {
	t0 := time.Now()

	// WLock: all new RLock() calls in handleWrite queue here until we release.
	n.renewMu.Lock()
	defer n.renewMu.Unlock()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, n.etcdctl,
		"--endpoints="+n.etcdEPs,
		"put", key, val,
	)
	cmd.Stdout = io.Discard
	cmd.Stderr = io.Discard
	_ = cmd.Run() // error ignored — stall duration is what matters, not success

	elapsed := time.Since(t0).Milliseconds()
	if elapsed >= n.minStallMs {
		atomic.AddInt64(&n.stallCount, 1)
		atomic.AddInt64(&n.stallMsTotal, elapsed)
		atomic.StoreInt64(&n.lastStallMs, elapsed)
	}
}

func main() {
	idFlag        := flag.String("id",           "",                              "Node ID (default: listen address)")
	listenFlag    := flag.String("listen",        ":19001",                        "HTTP listen address for client requests")
	replFlag      := flag.String("repl",          ":18001",                        "UDP listen port to absorb peer replication")
	peersFlag     := flag.String("peers",         "",                              "Comma-separated peer UDP replication addresses")
	etcdctlFlag   := flag.String("etcdctl",       "etcdctl",                       "Path to etcdctl binary")
	etcdFlag      := flag.String("etcd",          "http://127.0.0.1:2379",         "Comma-separated etcd HTTP endpoints")
	batchFlag     := flag.Int64("batch",          2000,                            "Checkpoint to etcd every N client ops (0=never)")
	pktFlag       := flag.Int("pkt",              1400,                            "UDP replication packet size in bytes")
	minStallMsFlag := flag.Int64("min-stall-ms", 50,                              "Checkpoint durations >= this (ms) are counted as stalls (0=count all)")
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
		id:          id,
		peers:       peerConns,
		pktSize:     *pktFlag,
		etcdctl:     *etcdctlFlag,
		etcdEPs:     *etcdFlag,
		batchSize:   *batchFlag,
		minStallMs:  *minStallMsFlag,
	}

	fmt.Printf("[node %s] listen=%s  repl=%s  peers=%d  batch=%d  min-stall-ms=%d  etcdctl=%s\n",
		id, *listenFlag, *replFlag, len(peerConns), *batchFlag, *minStallMsFlag, *etcdctlFlag)

	// Register this node in etcd (fire-and-forget — infrastructure write, not app checkpoint)
	go func() {
		cmd := exec.Command(*etcdctlFlag, "--endpoints="+*etcdFlag,
			"put", "/noise/nodes/"+id,
			fmt.Sprintf(`{"listen":%q,"repl":%q}`, *listenFlag, *replFlag),
		)
		cmd.Stdout = io.Discard
		cmd.Stderr = io.Discard
		_ = cmd.Run()
	}()

	// Periodic heartbeat: proves to etcd watchers the node is alive.
	// Fire-and-forget — this is infrastructure, not an application checkpoint.
	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for range ticker.C {
			hb := fmt.Sprintf(`{"ops":%d,"ts":%d}`,
				atomic.LoadInt64(&n.totalOps), time.Now().Unix())
			go func(v string) {
				cmd := exec.Command(*etcdctlFlag, "--endpoints="+*etcdFlag,
					"put", "/noise/heartbeat/"+id, v,
				)
				cmd.Stdout = io.Discard
				cmd.Stderr = io.Discard
				_ = cmd.Run()
			}(hb)
		}
	}()

	// Stats printer
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		prev := int64(0)
		for range ticker.C {
			cur    := atomic.LoadInt64(&n.totalOps)
			errs   := atomic.LoadInt64(&n.totalErrs)
			stalln := atomic.LoadInt64(&n.stallCount)
			stallms := atomic.LoadInt64(&n.stallMsTotal)
			lastms := atomic.LoadInt64(&n.lastStallMs)
			bsz    := n.batchSize
			etcdRate := int64(0)
			if bsz > 0 {
				etcdRate = (cur - prev) / bsz
			}
			fmt.Printf("[node %s] ops/s=%d  etcd_writes/s≈%d  total=%d  errs=%d  stalls=%d  stall_ms_total=%d  last_stall_ms=%d\n",
				id, cur-prev, etcdRate, cur, errs, stalln, stallms, lastms)
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
		go func() {
			cmd := exec.Command(*etcdctlFlag, "--endpoints="+*etcdFlag,
				"put", "/noise/nodes/"+id, `{"status":"down"}`,
			)
			cmd.Stdout = io.Discard
			cmd.Stderr = io.Discard
			_ = cmd.Run()
		}()
		time.Sleep(100 * time.Millisecond)
		srv.Close()
	}()

	fmt.Printf("[node %s] HTTP ready on %s\n", id, *listenFlag)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fmt.Fprintf(os.Stderr, "serve: %v\n", err)
		os.Exit(1)
	}
}
