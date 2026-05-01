#!/usr/bin/env python3
# scripts/analyze_results.py

import pandas as pd
import json
import sys
from pathlib import Path

def analyze_scenario(results_dir: str, scenario_json: str):
    scenario = json.loads(scenario_json)
    expected = scenario['expected']
    
    # Load timeseries data
    df = pd.read_csv(f"{results_dir}/timeseries.csv")
    
    # Calculate metrics
    total_elections = df['leader_changes'].iloc[-1] - df['leader_changes'].iloc[0]
    max_pending = df['proposals_pending'].max()
    total_failures = df['proposals_failed'].iloc[-1] - df['proposals_failed'].iloc[0]
    
    # Calculate success rate from workload output
    workload_files = list(Path(results_dir).glob("phase_*.json"))
    total_ops = 0
    successful_ops = 0
    latencies = []
    
    for f in workload_files:
        with open(f) as wf:
            data = json.load(wf)
            total_ops += data.get('total_operations', 0)
            successful_ops += data.get('successful_operations', 0)
            latencies.extend(data.get('latencies_ms', []))
    
    success_rate = successful_ops / total_ops if total_ops > 0 else 0
    p99_latency = sorted(latencies)[int(len(latencies) * 0.99)] if latencies else 0
    
    # Evaluate against expected
    results = {
        'scenario': scenario['name'],
        'elections': int(total_elections),
        'elections_pass': bool(total_elections <= expected['max_elections']),
        'p99_latency_ms': float(p99_latency),
        'latency_pass': bool(p99_latency <= expected['max_p99_latency_ms']),
        'success_rate': float(success_rate),
        'success_pass': bool(success_rate >= expected['min_success_rate']),
        'max_pending_proposals': int(max_pending),
        'total_failures': int(total_failures)
    }
    
    # Write results
    with open(f"{results_dir}/analysis.json", 'w') as f:
        json.dump(results, f, indent=2)
    
    # Print summary
    all_pass = results['elections_pass'] and results['latency_pass'] and results['success_pass']
    status = "PASS" if all_pass else "FAIL"
    
    print(f"\n{'='*50}")
    print(f"Scenario: {scenario['name']} - {status}")
    print(f"{'='*50}")
    print(f"Elections: {total_elections} (max: {expected['max_elections']}) - {'✓' if results['elections_pass'] else '✗'}")
    print(f"P99 Latency: {p99_latency:.2f}ms (max: {expected['max_p99_latency_ms']}ms) - {'✓' if results['latency_pass'] else '✗'}")
    print(f"Success Rate: {success_rate:.4f} (min: {expected['min_success_rate']}) - {'✓' if results['success_pass'] else '✗'}")
    
    return results

if __name__ == "__main__":
    results_dir = sys.argv[1]
    scenario_json = sys.argv[2]
    analyze_scenario(results_dir, scenario_json)