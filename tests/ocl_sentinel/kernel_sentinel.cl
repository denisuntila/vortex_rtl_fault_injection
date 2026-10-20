// kernel_sentinel.cl — sgemm with a sentinel lane per warp (ocl_sentinel test).
//
// Every work-item reads its operands through three offset tables built by the
// host: real lanes point at their row of A / column of B / element of C,
// sentinel lanes point at fixed data appended to A and B and at a check slot
// appended to C. There is NO condition in the kernel: every lane executes
// exactly the same instructions, so the sentinel follows the same
// instruction stream as the real lanes of its warp.
//
// With -DVX_CSR (Vortex) the kernel also stores the active thread mask
// (CSR 0xCC4, "tmask") next to each result; on other devices it stores -1.

#ifdef VX_CSR
#define READ_CSR(csr) ({ int _v; __asm__ volatile ("csrr %0, " #csr : "=r"(_v)); _v; })
#define CSR_TID   READ_CSR(0xCC0)
#define CSR_WID   READ_CSR(0xCC1)
#define CSR_CID   READ_CSR(0xCC2)
#define CSR_TMASK READ_CSR(0xCC4)
#else
#define CSR_TID   (-1)
#define CSR_WID   (-1)
#define CSR_CID   (-1)
#define CSR_TMASK (-1)
#endif

// Variants of the same computation: they differ only in how the inner loop is
// written, because the compiler's choice for the loop exit matters on Vortex.
// If the exit compares a per-thread value (e.g. a pointer), the backend sees a
// potentially divergent loop and inserts vx_pred_n (+ csrr tmask) in every
// iteration; with a counter starting from the uniform K it emits a plain bnez,
// as in the original sgemm. Selected at run time with ocl_sentinel -V <n>.

#define SGEMM_SENT_ARGS __global float *C, __global int *T,                  \
                        __global const float *A, __global const float *B,    \
                        __global const int *offA, __global const int *offB,  \
                        __global const int *offC, const int N, const int K

// v0: base pointers + index (first version: the exit became a pointer compare)
__kernel void sgemm_sent(SGEMM_SENT_ARGS)
{
  const int g = get_global_id(0);
  __global const float *a = A + offA[g];
  __global const float *b = B + offB[g];

  float sum = 0.0f;
  for (int k = 0; k < K; ++k)
    sum += a[k] * b[k * N];

  const int o = offC[g];
  C[o] = sum;
  T[o] = CSR_TMASK;
}

// v1: same indexing shape as the original kernel, A[row*K + k] * B[k*N + col],
//     with row*K and col replaced by the table entries
__kernel void sgemm_sent_v1(SGEMM_SENT_ARGS)
{
  const int g = get_global_id(0);
  const int oa = offA[g];
  const int ob = offB[g];

  float sum = 0.0f;
  for (int k = 0; k < K; ++k)
    sum += A[oa + k] * B[k * N + ob];

  const int o = offC[g];
  C[o] = sum;
  T[o] = CSR_TMASK;
}

// v2: explicit down-counter from the uniform K, pointers advanced by hand
__kernel void sgemm_sent_v2(SGEMM_SENT_ARGS)
{
  const int g = get_global_id(0);
  __global const float *a = A + offA[g];
  __global const float *b = B + offB[g];

  float sum = 0.0f;
  for (int k = K; k > 0; --k) {
    sum += *a * *b;
    a += 1;
    b += N;
  }

  const int o = offC[g];
  C[o] = sum;
  T[o] = CSR_TMASK;
}

// v3: like v1, unsigned arithmetic (the original host passes uint32 M/N/K)
__kernel void sgemm_sent_v3(SGEMM_SENT_ARGS)
{
  const uint g = get_global_id(0);
  const uint oa = offA[g];
  const uint ob = offB[g];
  const uint n = N, kk = K;

  float sum = 0.0f;
  for (uint k = 0; k < kk; ++k)
    sum += A[oa + k] * B[k * n + ob];

  const uint o = offC[g];
  C[o] = sum;
  T[o] = CSR_TMASK;
}

// Mapping probe: where does work-item g run? (core, warp, lane, mask)
__kernel void probe(__global int *out)
{
  const int g = get_global_id(0);
  out[4 * g + 0] = CSR_CID;
  out[4 * g + 1] = CSR_WID;
  out[4 * g + 2] = CSR_TID;
  out[4 * g + 3] = CSR_TMASK;
}
