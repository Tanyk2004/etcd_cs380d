// scenarios/config.go
package scenarios

type TestScenario struct {
    Name        string        `json:"name"`
    Description string        `json:"description"`
    Duration    time.Duration `json:"duration"`
    Phases      []Phase       `json:"phases"`
    Network     NetworkConfig `json:"network"`
    Expected    ExpectedResults `json:"expected"`
}

type Phase struct {
    Duration    time.Duration `json:"duration"`
    Workers     int           `json:"workers"`
    QPS         int           `json:"qps"`
    ValueSize   int           `json:"value_size"`    // bytes
    ReadRatio   float64       `json:"read_ratio"`    // 0.0 = all writes
    KeyRange    int           `json:"key_range"`     // number of unique keys
}

type NetworkConfig struct {
    Latency      string `json:"latency"`       // "0ms", "10ms", "50ms"
    Jitter       string `json:"jitter"`        // "0ms", "5ms"
    PacketLoss   float64 `json:"packet_loss"`  // 0.0 - 1.0
    Bandwidth    string `json:"bandwidth"`     // "" = unlimited, "100mbit"
}

type ExpectedResults struct {
    MaxElections     int     `json:"max_elections"`
    MaxP99LatencyMs  float64 `json:"max_p99_latency_ms"`
    MinSuccessRate   float64 `json:"min_success_rate"`
}