__kernel void sgemm(__global float* C,
                     __global const float* A,
                     __global const float* B,
                     const uint M,
                     const uint N,
                     const uint K) {
  int col = get_global_id(0); // 0..N-1
  int row = get_global_id(1); // 0..M-1

  float sum = 0.0f;
  for (uint k = 0; k < K; ++k) {
    sum += A[row * K + k] * B[k * N + col];
  }
  C[row * N + col] = sum;
}
