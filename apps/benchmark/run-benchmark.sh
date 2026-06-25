#!/bin/bash

SSH_CMD="ssh -i ~/.ssh/5G-lab-2026 -o StrictHostKeyChecking=no ubuntu@130.235.32.171"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "╔═══════════════════════════════════════════════════════════════════════════════════╗"
echo "║                         GPU vs CPU  Benchmark Suite                              ║"
echo "╚═══════════════════════════════════════════════════════════════════════════════════╝"
echo ""

# --- Deploy ---
echo "[1/4] Deploying benchmark jobs..."
$SSH_CMD "kubectl delete jobs -l bench-suite=gpu-cpu-compare 2>/dev/null || true; kubectl delete configmap benchmark-scripts 2>/dev/null || true"
cat "$SCRIPT_DIR/gpu-benchmark.yaml" | $SSH_CMD "kubectl apply -f - 2>/dev/null"
echo "       7 jobs submitted (3 GPU, 4 CPU)"
echo ""

# --- Wait ---
echo "[2/4] Waiting for completion..."
while true; do
  counts=$($SSH_CMD "
    total=\$(kubectl get jobs -l bench-suite=gpu-cpu-compare --no-headers 2>/dev/null | wc -l)
    done=\$(kubectl get jobs -l bench-suite=gpu-cpu-compare --no-headers 2>/dev/null | grep -c Complete || echo 0)
    echo \"\$done \$total\"
  ")
  done_count=$(echo $counts | awk '{print $1}')
  total=$(echo $counts | awk '{print $2}')
  if [ "${total:-0}" -gt 0 ] && [ "${done_count:-0}" -eq "${total:-0}" ]; then
    break
  fi
  printf "\r       Completed: %s/%s " "$done_count" "$total"
  sleep 5
done
printf "\r       Completed: %s/%s \n" "$total" "$total"
echo ""

# --- Collect ---
echo "[3/4] Collecting results..."
results=$($SSH_CMD "
echo 'MATRIX_GPU:'
kubectl logs job/bench-matrix-gpu 2>/dev/null | grep workload || true
echo 'MATRIX_CPU:'
kubectl logs job/bench-matrix-cpu 2>/dev/null | grep workload || true
echo 'FFT_GPU:'
kubectl logs job/bench-fft-gpu 2>/dev/null | grep workload || true
echo 'FFT_CPU:'
kubectl logs job/bench-fft-cpu 2>/dev/null | grep workload || true
echo 'INFERENCE_GPU:'
kubectl logs job/bench-inference-gpu 2>/dev/null | grep workload || true
echo 'INFERENCE_CPU:'
kubectl logs job/bench-inference-cpu 2>/dev/null | grep workload || true
echo 'SORT_CPU:'
kubectl logs job/bench-sortcompress-cpu 2>/dev/null | grep workload || true
echo 'END:'
")

extract() {
  echo "$results" | sed -n "/^${1}:/,/^[A-Z]/p" | grep -o "\"${2}\": [0-9.]*" | grep -o '[0-9.]*' | head -1
}

gpu_matrix_gflops=$(extract MATRIX_GPU gflops)
cpu_matrix_gflops=$(extract MATRIX_CPU gflops)
gpu_matrix_time=$(extract MATRIX_GPU elapsed_s)
cpu_matrix_time=$(extract MATRIX_CPU elapsed_s)

gpu_fft_sps=$(extract FFT_GPU signals_per_sec)
cpu_fft_sps=$(extract FFT_CPU signals_per_sec)
gpu_fft_time=$(extract FFT_GPU elapsed_s)
cpu_fft_time=$(extract FFT_CPU elapsed_s)

gpu_inf_ips=$(extract INFERENCE_GPU images_per_sec)
cpu_inf_ips=$(extract INFERENCE_CPU images_per_sec)
gpu_inf_time=$(extract INFERENCE_GPU elapsed_s)
cpu_inf_time=$(extract INFERENCE_CPU elapsed_s)

cpu_sort_time=$(extract SORT_CPU sort_time_s)
cpu_compress_tp=$(extract SORT_CPU compress_throughput_mbs)

matrix_speedup=$(echo "scale=0; ${gpu_matrix_gflops:-0} / ${cpu_matrix_gflops:-1}" | bc 2>/dev/null || echo "?")
fft_speedup=$(echo "scale=0; ${gpu_fft_sps:-0} / ${cpu_fft_sps:-1}" | bc 2>/dev/null || echo "?")
inf_speedup=$(echo "scale=0; ${gpu_inf_ips:-0} / ${cpu_inf_ips:-1}" | bc 2>/dev/null || echo "?")

# --- Display ---
echo ""
echo "╔═══════════════════════════════════════════════════════════════════════════════════╗"
echo "║                              RESULTS                                             ║"
echo "╠═══════════════════════════════════════════════════════════════════════════════════╣"
echo "║  GPU: NVIDIA L40S (48GB)  gpuserver01-5g      128 cores / 768 GB RAM             ║"
echo "║  CPU: k8sv2-2 worker node                      20 cores /  15 GB RAM (8 used)    ║"
echo "╚═══════════════════════════════════════════════════════════════════════════════════╝"
echo ""
echo "┌─────────────────────┬──────────┬────────────────┬──────────────┬─────────────────┐"
echo "│ WORKLOAD            │ BACKEND  │ THROUGHPUT     │ TIME         │ SPEEDUP         │"
echo "├─────────────────────┼──────────┼────────────────┼──────────────┼─────────────────┤"
printf "│ %-19s │ \033[1;35m%-8s\033[0m │ %12s   │ %9ss  │                 │\n" "Matrix 8192x8192" "GPU" "${gpu_matrix_gflops} GF/s" "$gpu_matrix_time"
printf "│ %-19s │ %-8s │ %12s   │ %9ss  │ \033[1;32m%4sx GPU\033[0m      │\n" "(10 iterations)" "CPU" "${cpu_matrix_gflops} GF/s" "$cpu_matrix_time" "$matrix_speedup"
echo "├─────────────────────┼──────────┼────────────────┼──────────────┼─────────────────┤"
printf "│ %-19s │ \033[1;35m%-8s\033[0m │ %12s   │ %9ss  │                 │\n" "Batch FFT" "GPU" "${gpu_fft_sps} sig/s" "$gpu_fft_time"
printf "│ %-19s │ %-8s │ %12s   │ %9ss  │ \033[1;32m%4sx GPU\033[0m      │\n" "(2k signals x 32k)" "CPU" "${cpu_fft_sps} sig/s" "$cpu_fft_time" "$fft_speedup"
echo "├─────────────────────┼──────────┼────────────────┼──────────────┼─────────────────┤"
printf "│ %-19s │ \033[1;35m%-8s\033[0m │ %12s   │ %9ss  │                 │\n" "CNN Inference" "GPU" "${gpu_inf_ips} img/s" "$gpu_inf_time"
printf "│ %-19s │ %-8s │ %12s   │ %9ss  │ \033[1;32m%4sx GPU\033[0m      │\n" "(224x224 images)" "CPU" "${cpu_inf_ips} img/s" "$cpu_inf_time" "$inf_speedup"
echo "├─────────────────────┼──────────┼────────────────┼──────────────┼─────────────────┤"
printf "│ %-19s │ \033[1;36m%-8s\033[0m │                │ %9ss  │ \033[1;36mCPU strength\033[0m    │\n" "Sort 20M int64" "CPU" "$cpu_sort_time"
printf "│ %-19s │ \033[1;36m%-8s\033[0m │ %10s MB/s │              │ \033[1;36msequential\033[0m      │\n" "zlib Compress" "CPU" "$cpu_compress_tp"
echo "└─────────────────────┴──────────┴────────────────┴──────────────┴─────────────────┘"
echo ""
echo "  GF/s = GFLOPS    sig/s = signals/sec    img/s = images/sec"
echo ""

# --- Cleanup ---
echo "[4/4] Cleaning up jobs..."
$SSH_CMD "kubectl delete jobs -l bench-suite=gpu-cpu-compare 2>/dev/null || true" > /dev/null
echo "       Done."
echo ""
