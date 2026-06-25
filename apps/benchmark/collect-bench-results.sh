#!/bin/bash
SSH="ssh -i ~/.ssh/5G-lab-2026 -o StrictHostKeyChecking=no ubuntu@130.235.32.171"

# Wait for all jobs to complete
echo "Waiting for benchmark jobs to complete..."
while true; do
  status=$($SSH "kubectl get jobs -l bench-suite=gpu-cpu-compare --no-headers 2>/dev/null" | awk '{print $2}')
  total=$(echo "$status" | wc -l | tr -d ' ')
  done_count=$(echo "$status" | grep -c "1/1")
  if [ "$done_count" -eq "$total" ] && [ "$total" -gt 0 ]; then
    break
  fi
  printf "\r  Completed: %d/%d" "$done_count" "$total"
  sleep 5
done
echo ""
echo ""

# Collect all results via single SSH
results=$($SSH "
echo 'MATRIX_GPU:'; kubectl logs job/bench-matrix-gpu 2>/dev/null | grep '{\"workload'
echo 'MATRIX_CPU:'; kubectl logs job/bench-matrix-cpu 2>/dev/null | grep '{\"workload'
echo 'FFT_GPU:'; kubectl logs job/bench-fft-gpu 2>/dev/null | grep '{\"workload'
echo 'FFT_CPU:'; kubectl logs job/bench-fft-cpu 2>/dev/null | grep '{\"workload'
echo 'INFERENCE_GPU:'; kubectl logs job/bench-inference-gpu 2>/dev/null | grep '{\"workload'
echo 'INFERENCE_CPU:'; kubectl logs job/bench-inference-cpu 2>/dev/null | grep '{\"workload'
echo 'SORT_CPU:'; kubectl logs job/bench-sortcompress-cpu 2>/dev/null | grep '{\"workload'
")

# Parse results
gpu_matrix_gflops=$(echo "$results" | sed -n '/^MATRIX_GPU:/,/^[A-Z]/p' | grep -o '"gflops": [0-9.]*' | grep -o '[0-9.]*')
cpu_matrix_gflops=$(echo "$results" | sed -n '/^MATRIX_CPU:/,/^[A-Z]/p' | grep -o '"gflops": [0-9.]*' | grep -o '[0-9.]*')
gpu_matrix_time=$(echo "$results" | sed -n '/^MATRIX_GPU:/,/^[A-Z]/p' | grep -o '"elapsed_s": [0-9.]*' | grep -o '[0-9.]*')
cpu_matrix_time=$(echo "$results" | sed -n '/^MATRIX_CPU:/,/^[A-Z]/p' | grep -o '"elapsed_s": [0-9.]*' | grep -o '[0-9.]*')

gpu_fft_sps=$(echo "$results" | sed -n '/^FFT_GPU:/,/^[A-Z]/p' | grep -o '"signals_per_sec": [0-9.]*' | grep -o '[0-9.]*')
cpu_fft_sps=$(echo "$results" | sed -n '/^FFT_CPU:/,/^[A-Z]/p' | grep -o '"signals_per_sec": [0-9.]*' | grep -o '[0-9.]*')
gpu_fft_time=$(echo "$results" | sed -n '/^FFT_GPU:/,/^[A-Z]/p' | grep -o '"elapsed_s": [0-9.]*' | grep -o '[0-9.]*')
cpu_fft_time=$(echo "$results" | sed -n '/^FFT_CPU:/,/^[A-Z]/p' | grep -o '"elapsed_s": [0-9.]*' | grep -o '[0-9.]*')

gpu_inf_ips=$(echo "$results" | sed -n '/^INFERENCE_GPU:/,/^[A-Z]/p' | grep -o '"images_per_sec": [0-9.]*' | grep -o '[0-9.]*')
cpu_inf_ips=$(echo "$results" | sed -n '/^INFERENCE_CPU:/,/^[A-Z]/p' | grep -o '"images_per_sec": [0-9.]*' | grep -o '[0-9.]*')
gpu_inf_time=$(echo "$results" | sed -n '/^INFERENCE_GPU:/,/^[A-Z]/p' | grep -o '"elapsed_s": [0-9.]*' | grep -o '[0-9.]*')
cpu_inf_time=$(echo "$results" | sed -n '/^INFERENCE_CPU:/,/^[A-Z]/p' | grep -o '"elapsed_s": [0-9.]*' | grep -o '[0-9.]*')

cpu_sort_time=$(echo "$results" | sed -n '/^SORT_CPU:/,/^$/p' | grep -o '"sort_time_s": [0-9.]*' | grep -o '[0-9.]*')
cpu_compress_tp=$(echo "$results" | sed -n '/^SORT_CPU:/,/^$/p' | grep -o '"compress_throughput_mbs": [0-9.]*' | grep -o '[0-9.]*')

# Compute speedups
matrix_speedup=$(echo "scale=0; $gpu_matrix_gflops / $cpu_matrix_gflops" | bc 2>/dev/null)
fft_speedup=$(echo "scale=0; $gpu_fft_sps / $cpu_fft_sps" | bc 2>/dev/null)
inf_speedup=$(echo "scale=0; $gpu_inf_ips / $cpu_inf_ips" | bc 2>/dev/null)

# Print results
echo "╔═══════════════════════════════════════════════════════════════════════════════════╗"
echo "║                         GPU vs CPU  Benchmark Results                            ║"
echo "╠═══════════════════════════════════════════════════════════════════════════════════╣"
echo "║  GPU: NVIDIA L40S (48GB)  gpuserver01-5g      128 cores / 768 GB RAM             ║"
echo "║  CPU: k8sv2-2 worker node                      20 cores /  15 GB RAM (8 used)    ║"
echo "╚═══════════════════════════════════════════════════════════════════════════════════╝"
echo ""
echo "┌─────────────────────┬──────────┬──────────────┬──────────────┬──────────────────┐"
echo "│ WORKLOAD            │ BACKEND  │ THROUGHPUT   │ TIME         │ SPEEDUP          │"
echo "├─────────────────────┼──────────┼──────────────┼──────────────┼──────────────────┤"
printf "│ %-19s │ %-8s │ %10s   │ %10ss │                  │\n" "Matrix 8192x8192" "GPU" "${gpu_matrix_gflops} GF" "$gpu_matrix_time"
printf "│ %-19s │ %-8s │ %10s   │ %10ss │ \033[1;32m%sx faster\033[0m      │\n" "(10 iterations)" "CPU" "${cpu_matrix_gflops} GF" "$cpu_matrix_time" "$matrix_speedup"
echo "├─────────────────────┼──────────┼──────────────┼──────────────┼──────────────────┤"
printf "│ %-19s │ %-8s │ %10s   │ %10ss │                  │\n" "Batch FFT" "GPU" "${gpu_fft_sps} s/s" "$gpu_fft_time"
printf "│ %-19s │ %-8s │ %10s   │ %10ss │ \033[1;32m%sx faster\033[0m       │\n" "(signals x 32k)" "CPU" "${cpu_fft_sps} s/s" "$cpu_fft_time" "$fft_speedup"
echo "├─────────────────────┼──────────┼──────────────┼──────────────┼──────────────────┤"
printf "│ %-19s │ %-8s │ %10s   │ %10ss │                  │\n" "CNN Inference" "GPU" "${gpu_inf_ips} i/s" "$gpu_inf_time"
printf "│ %-19s │ %-8s │ %10s   │ %10ss │ \033[1;32m%sx faster\033[0m      │\n" "(224x224 images)" "CPU" "${cpu_inf_ips} i/s" "$cpu_inf_time" "$inf_speedup"
echo "├─────────────────────┼──────────┼──────────────┼──────────────┼──────────────────┤"
printf "│ %-19s │ %-8s │ %10s   │ %10ss │ \033[1;36mCPU strength\033[0m     │\n" "Sort 20M integers" "CPU" "" "$cpu_sort_time"
printf "│ %-19s │ %-8s │ %8s MB/s │              │ \033[1;36msequential work\033[0m  │\n" "zlib Compress" "CPU" "$cpu_compress_tp"
echo "└─────────────────────┴──────────┴──────────────┴──────────────┴──────────────────┘"
echo ""
echo "  Key: GF=GFLOPS  s/s=signals/sec  i/s=images/sec"
echo ""
