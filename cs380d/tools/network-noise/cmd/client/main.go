// noise-client: HTTP load generator for noise-node clusters.
//
// Replaces traffic-sim for NIC-saturation scenarios.  Sends POST /write
// requests to noise-node instances; those nodes do the UDP peer replication
// and occasional etcd metadata writes.  No etcd traffic originates here.
//
// Stats output is intentionally formatted to match traffic-sim's [stats] lines
// so parse_tester_stats.py can parse the logs unchanged.
//
// USAGE
//   noise-client \
//     -nodes  http://127.0.0.1:19001,http://127.0.0.1:19002,http://127.0.0.1:19003 \
//     -qps    5000   \
//     -size   4096   \
//     -workers 50    \
//     -dur    120s
package main

import (
	"bytes"
	"flag"
	"fmt"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

func main() {
	nodesFlag   := flag.String("nodes",   "http://127.0.0.1:19001,http://127.0.0.1:19002,http://127.0.0.1:19003",
	                                       "Comma-separated noise-node HTTP endpoints")
	qpsFlag     := flag.Int64("qps",      1000,  "Target total requests per second across all workers")
	sizeFlag    := flag.Int("size",        4096,  "Request body size in bytes (also sets UDP replication pkt size on node side)")
	workersFlag := flag.Int("workers",     20,    "Concurrent HTTP workers")
	durFlag     := flag.Duration("dur",    0,     "Run duration; 0 = until SIGINT/SIGTERM")
	profileFlag := flag.String("profile",  "noise", "Profile label printed in [stats] lines")
	stallMsFlag := flag.Int64("stall-ms",  150,   "Latency >= this (ms) counted as election stall; set to election_timeout_ms")
	flag.Parse()

	nodes := strings.Split(*nodesFlag, ",")
	for i, n := range nodes {
		nodes[i] = strings.TrimSpace(n)
	}

	// Pre-allocate payload so all workers share the same read-only buffer.
	payload := make([]byte, *sizeFlag)

	// Per-worker interval to achieve total QPS.
	ppsPerWorker := float64(*qpsFlag) / float64(*workersFlag)
	interval := time.Duration(float64(time.Second) / ppsPerWorker)

	stallThreshold := time.Duration(*stallMsFlag) * time.Millisecond

	// ── Counters (atomic) ─────────────────────────────────────────────────────
	var (
		totalOps   int64
		totalErrs  int64
		latU1ms    int64 // ops completing < 1ms
		latU10ms   int64 // ops completing < 10ms
		latU100ms  int64 // ops completing < 100ms
		latSlow    int64 // ops taking ≥ 100ms (election / queue delay)
		latElect   int64 // ops with lat >= stall-ms (election stall indicator)
	)

	// ── Stop signal ───────────────────────────────────────────────────────────
	stop := make(chan struct{})
	if *durFlag > 0 {
		go func() { time.Sleep(*durFlag); close(stop) }()
	} else {
		ch := make(chan os.Signal, 1)
		signal.Notify(ch, syscall.SIGINT, syscall.SIGTERM)
		go func() { <-ch; close(stop) }()
	}

	// ── Stats printer ─────────────────────────────────────────────────────────
	// Matches traffic-sim's [stats] format so parse_tester_stats.py works:
	//   [stats] profile=<P> ops/s=<N> put=<N> get=<N> del=<N> txn=<N> lease=<N>
	//           err=<N> | lat(u1ms=<N> u10ms=<N> u100ms=<N> slow=<N>)
	go func() {
		prevOps, prevErrs                          := int64(0), int64(0)
		prevU1, prevU10, prevU100, prevSlow, prevElect := int64(0), int64(0), int64(0), int64(0), int64(0)
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-stop:
				return
			case <-ticker.C:
			}
			ops   := atomic.LoadInt64(&totalOps)
			errs  := atomic.LoadInt64(&totalErrs)
			u1    := atomic.LoadInt64(&latU1ms)
			u10   := atomic.LoadInt64(&latU10ms)
			u100  := atomic.LoadInt64(&latU100ms)
			slow  := atomic.LoadInt64(&latSlow)
			elect := atomic.LoadInt64(&latElect)

			dOps   := ops - prevOps
			dErrs  := errs - prevErrs
			dU1    := u1 - prevU1
			dU10   := u10 - prevU10
			dU100  := u100 - prevU100
			dSlow  := slow - prevSlow
			dElect := elect - prevElect

			// put = successful ops; get/del/txn/lease = 0 (this client only writes)
			fmt.Printf(
				"[stats] ts=%-10d profile=%-15s ops/s=%-6d put=%-5d get=0     del=0     txn=0     lease=0     err=%-4d | lat(u1ms=%d u10ms=%d u100ms=%d slow=%d stalled=%d)\n",
				time.Now().Unix(), *profileFlag, dOps, dOps-dErrs, dErrs,
				dU1, dU10, dU100, dSlow, dElect,
			)

			prevOps, prevErrs = ops, errs
			prevU1, prevU10, prevU100, prevSlow, prevElect = u1, u10, u100, slow, elect
		}
	}()

	// ── HTTP client with connection pooling ───────────────────────────────────
	// MaxIdleConnsPerHost = workers so we don't re-establish connections on
	// every request (which would itself add TCP-handshake load).
	client := &http.Client{
		Timeout: 10 * time.Second,
		Transport: &http.Transport{
			MaxIdleConnsPerHost:   *workersFlag + 4,
			MaxConnsPerHost:       *workersFlag * 2,
			IdleConnTimeout:       30 * time.Second,
			ResponseHeaderTimeout: 8 * time.Second,
		},
	}

	// ── Workers ───────────────────────────────────────────────────────────────
	var wg sync.WaitGroup
	for i := 0; i < *workersFlag; i++ {
		wg.Add(1)
		go func(workerID int) {
			defer wg.Done()
			rng := rand.New(rand.NewSource(int64(workerID) * 6364136223846793005))
			ticker := time.NewTicker(interval)
			defer ticker.Stop()

			for {
				select {
				case <-stop:
					return
				case <-ticker.C:
				}

				target := nodes[rng.Intn(len(nodes))]
				t0 := time.Now()

				resp, err := client.Post(
					target+"/write",
					"application/octet-stream",
					bytes.NewReader(payload), // bytes.NewReader does not copy
				)
				lat := time.Since(t0)

				atomic.AddInt64(&totalOps, 1)

				if err != nil {
					atomic.AddInt64(&totalErrs, 1)
					atomic.AddInt64(&latSlow, 1)
					if lat >= stallThreshold {
						atomic.AddInt64(&latElect, 1)
					}
					continue
				}
				resp.Body.Close()
				if resp.StatusCode != http.StatusOK {
					atomic.AddInt64(&totalErrs, 1)
					atomic.AddInt64(&latSlow, 1)
					if lat >= stallThreshold {
						atomic.AddInt64(&latElect, 1)
					}
					continue
				}

				switch {
				case lat < time.Millisecond:
					atomic.AddInt64(&latU1ms, 1)
				case lat < 10*time.Millisecond:
					atomic.AddInt64(&latU10ms, 1)
				case lat < 100*time.Millisecond:
					atomic.AddInt64(&latU100ms, 1)
				default:
					atomic.AddInt64(&latSlow, 1)
				}
				if lat >= stallThreshold {
					atomic.AddInt64(&latElect, 1)
				}
			}
		}(i)
	}

	wg.Wait()
	fmt.Printf("[noise-client] done. total_ops=%d  total_errs=%d\n",
		atomic.LoadInt64(&totalOps), atomic.LoadInt64(&totalErrs))
}
