#include <stdio.h>
#include <stdlib.h>
#include <assert.h>
#define CL_TARGET_OPENCL_VERSION 120
#include <CL/opencl.h>
#include <string.h>
#include <cstring>
#include <time.h>
#include <unistd.h>
#include <chrono>
#include <vector>
#include <iostream>
#include <cmath>
#include <cstdint>
#include <csignal>

#define FLOAT_ULP 6

void output_crash_json(const char* expr, int code, const char* details) {
  const char* msg = details ? details : "Runtime execution error";
  size_t msg_len = std::strlen(msg);

  std::cout << "{\n";
  std::cout << "  \"status\": \"crash\",\n";
  std::cout << "  \"crash\": {\n";
  std::cout << "    \"signal\": \"" << (expr ? expr : "SIG_UNKNOWN") << " (code " << code << ")\",\n";
  std::cout << "    \"message\": \"" << msg << "\",\n";
  std::cout << "    \"message_size\": " << msg_len << "\n";
  std::cout << "  }\n";
  std::cout << "}\n";
}

void cleanup(); // Forward declaration

// Gestore dei segnali fatali (SIGSEGV, SIGABRT, ecc.)
void signal_handler(int sig) {
  cleanup();
  output_crash_json(nullptr, sig, strsignal(sig));
  _exit(sig);
}

#define CL_CHECK(_expr)                                                     \
   do {                                                                     \
     cl_int _err = _expr;                                                   \
     if (_err == CL_SUCCESS)                                                \
       break;                                                               \
     cleanup();                                                             \
     output_crash_json(#_expr, _err, "OpenCL API call failed");             \
     exit(-1);                                                              \
   } while (false)

#define CL_CHECK2(_expr)                                                    \
   ({                                                                       \
     cl_int _err = CL_INVALID_VALUE;                                        \
     decltype(_expr) _ret = _expr;                                          \
     if (_err != CL_SUCCESS) {                                              \
       cleanup();                                                           \
       output_crash_json(#_expr, _err, "OpenCL API call failed");           \
       exit(-1);                                                            \
     }                                                                      \
     _ret;                                                                  \
   })

struct Divergence {
  uint32_t index;
  float actual;
  float expected;
  int hamming_distance;
};

// Calcolo della distanza di Hamming bitwise per float (32-bit)
template <typename T>
int calculate_hamming_distance(T a, T b) {
  static_assert(sizeof(T) == 4 || sizeof(T) == 8, "Unsupported data size for Hamming distance");
  if constexpr (sizeof(T) == 4) {
    uint32_t bits_a, bits_b;
    std::memcpy(&bits_a, &a, sizeof(T));
    std::memcpy(&bits_b, &b, sizeof(T));
    return __builtin_popcount(bits_a ^ bits_b);
  } else {
    uint64_t bits_a, bits_b;
    std::memcpy(&bits_a, &a, sizeof(T));
    std::memcpy(&bits_b, &b, sizeof(T));
    return __builtin_popcountll(bits_a ^ bits_b);
  }
}

static int read_kernel_file(const char* filename, uint8_t** data, size_t* size) {
  if (nullptr == filename || nullptr == data || 0 == size)
    return -1;

  FILE* fp = fopen(filename, "r");
  if (NULL == fp) {
    return -1;
  }

  fseek(fp , 0 , SEEK_END);
  long fsize = ftell(fp);
  rewind(fp);

  *data = (uint8_t*)malloc(fsize);
  *size = fread(*data, 1, fsize, fp);

  fclose(fp);
  return 0;
}

static bool compare_equal(float a, float b) {
  union fi_t { float f; int32_t i; };
  fi_t fa, fb;
  fa.f = a;
  fb.f = b;
  auto d = std::abs(fa.i - fb.i);
  return d <= FLOAT_ULP;
}

// C = A * B
static void matmul_cpu(float *C, const float *A, const float *B, int32_t M, int32_t N, int32_t K) {
  for (int32_t row = 0; row < M; ++row) {
    for (int32_t col = 0; col < N; ++col) {
      float sum = 0.0f;
      for (int32_t k = 0; k < K; ++k) {
        sum += A[row * K + k] * B[k * N + col];
      }
      C[row * N + col] = sum;
    }
  }
}

cl_device_id device_id = NULL;
cl_context context = NULL;
cl_command_queue commandQueue = NULL;
cl_program program = NULL;
cl_kernel kernel = NULL;
cl_mem a_memobj = NULL;
cl_mem b_memobj = NULL;
cl_mem c_memobj = NULL;
cl_mem t_memobj = NULL;
cl_mem oa_memobj = NULL;
cl_mem ob_memobj = NULL;
cl_mem oc_memobj = NULL;
uint8_t* kernel_bin = NULL;

void cleanup() {
  if (commandQueue) { clReleaseCommandQueue(commandQueue); commandQueue = NULL; }
  if (kernel) { clReleaseKernel(kernel); kernel = NULL; }
  if (program) { clReleaseProgram(program); program = NULL; }
  cl_mem* mems[] = {&a_memobj, &b_memobj, &c_memobj, &t_memobj, &oa_memobj, &ob_memobj, &oc_memobj};
  for (cl_mem* m : mems) { if (*m) { clReleaseMemObject(*m); *m = NULL; } }
  if (context) { clReleaseContext(context); context = NULL; }
  if (device_id) { clReleaseDevice(device_id); device_id = NULL; }
  if (kernel_bin) { free(kernel_bin); kernel_bin = NULL; }
}

void register_signal_handlers() {
  struct sigaction sa;
  sa.sa_handler = signal_handler;
  sigemptyset(&sa.sa_mask);
  sa.sa_flags = 0;

  sigaction(SIGSEGV, &sa, NULL);
  sigaction(SIGABRT, &sa, NULL);
  sigaction(SIGFPE,  &sa, NULL);
  sigaction(SIGILL,  &sa, NULL);
  sigaction(SIGBUS,  &sa, NULL);
}

int M = 32;
int N = 32;
int K = 32;
int W = 8;           // hardware threads per warp; 0 = no sentinel (identity tables)
int R = 16;          // round the work-item count up to a multiple of R (cores * W)
bool probe_mode = false;
bool use_csr = true; // -x: device without Vortex CSRs (e.g. pocl CPU, for testing)
int variant = 1;     // -V: kernel variant (see kernel_sentinel.cl); 1 = no vx_pred in the loop
int recovery = 0;    // -e N: up to N relaunches of the warps whose sentinel fired (0 = off)

static void parse_args(int argc, char **argv) {
  int c;
  while ((c = getopt(argc, argv, "m:n:k:w:r:V:e:Pxh")) != -1) {
    switch (c) {
    case 'm': M = atoi(optarg); break;
    case 'n': N = atoi(optarg); break;
    case 'k': K = atoi(optarg); break;
    case 'w': W = atoi(optarg); break;
    case 'r': R = atoi(optarg); break;
    case 'P': probe_mode = true; break;
    case 'V': variant = atoi(optarg); break;
    case 'e': recovery = atoi(optarg); break;
    case 'x': use_csr = false; break;
    case 'h':
    default:
      output_crash_json(nullptr, -1, "Invalid command-line arguments");
      exit(-1);
    }
  }
  if (M <= 0 || N <= 0 || K <= 0 || W < 0 || W == 1 || R <= 0 || variant < 0 || variant > 3 || recovery < 0 || (W > 0 && R % W != 0)) {
    output_crash_json(nullptr, -1, "Invalid geometry: need W = 0 or W >= 2, and R a multiple of W");
    exit(-1);
  }
}

// ---------------------------------------------------------------------------
// Geometry. Work-item p runs on lane p % W of warp p / W (to be verified on
// the device with -P). The last lane of every warp is a sentinel; the other
// W-1 lanes take the real elements in order:
//   W = 4:  warp0 (0 1 2 s)  warp1 (3 4 5 s)  warp2 (6 7 - s)
// Slots past the last real element ('-') and the work-items added to reach a
// multiple of R are sentinels too, so every warp is full and convergent.
// ---------------------------------------------------------------------------
struct Geometry {
  uint32_t P = 0;                       // work-items launched
  uint32_t S = 0;                       // sentinel slots
  std::vector<int32_t> offA, offB, offC;
  std::vector<uint32_t> slot_warp;      // sentinel slot s -> warp (p / W) it checks
  std::vector<std::vector<uint32_t>> warp_items;  // warp -> positions j (in elems) of its real lanes
};

// elems[j] = element of C (row * N + col) computed by the j-th real lane;
// out_base: real lane j writes to out_base ? j : elems[j]  (C2 compact / C in place);
// sentinel slots are c_chk, c_chk + 1, ...
static Geometry build_geometry(const std::vector<uint32_t>& elems, bool compact,
                               uint32_t a_fix, uint32_t b_fix, uint32_t c_chk) {
  Geometry g;
  const uint32_t real = elems.size();
  uint32_t warps = 0;
  if (W == 0) {
    g.P = real;
  } else {
    warps = (real + (W - 2)) / (W - 1);
    g.P = warps * W;
    g.P = (g.P + R - 1) / R * R;
    g.warp_items.resize(g.P / W);
  }
  g.offA.resize(g.P); g.offB.resize(g.P); g.offC.resize(g.P);
  for (uint32_t p = 0; p < g.P; ++p) {
    bool sentinel;
    uint32_t j;
    if (W == 0) {
      sentinel = false;
      j = p;
    } else {
      uint32_t lane = p % W, warp = p / W;
      j = warp * (W - 1) + lane;
      sentinel = (lane == (uint32_t)W - 1) || (j >= real);
    }
    if (sentinel) {
      g.offA[p] = a_fix;
      g.offB[p] = b_fix;
      g.offC[p] = c_chk + g.S++;
      g.slot_warp.push_back(p / W);
    } else {
      uint32_t i = elems[j];
      uint32_t row = i / N, col = i % N;
      g.offA[p] = row * K;
      g.offB[p] = col;
      g.offC[p] = compact ? j : i;
      if (W > 0) g.warp_items[p / W].push_back(j);
    }
  }
  return g;
}

static const uint32_t POISON_BITS = 0x7fc0deadu;   // NaN: "never written"
static const float FIX_PAD = 1000.0f;              // fixed-data padding: misaddressing shows up

static uint32_t fbits(float f) { uint32_t u; std::memcpy(&u, &f, 4); return u; }
static float bitsf(uint32_t u) { float f; std::memcpy(&f, &u, 4); return f; }

static void load_program(const char* options) {
  size_t kernel_size;
  // own file name, so it can live next to ocl_test's kernel.cl in the same directory
  const char* kfile = std::getenv("SENTINEL_KERNEL_FILE");
  if (!kfile) kfile = "kernel_sentinel.cl";
  if (0 != read_kernel_file(kfile, &kernel_bin, &kernel_size)) {
    cleanup();
    output_crash_json("read_kernel_file", -1, "Failed to load kernel file");
    exit(-1);
  }
  program = CL_CHECK2(clCreateProgramWithSource(
    context, 1, (const char**)&kernel_bin, &kernel_size, &_err));
  CL_CHECK(clBuildProgram(program, 1, &device_id, options, NULL, NULL));
}

// -P: where does every work-item run? Checks the p % W hypothesis.
static int run_probe(uint32_t P) {
  kernel = CL_CHECK2(clCreateKernel(program, "probe", &_err));
  std::vector<int32_t> out(4 * P, -2);
  c_memobj = CL_CHECK2(clCreateBuffer(context, CL_MEM_WRITE_ONLY, out.size() * 4, NULL, &_err));
  CL_CHECK(clSetKernelArg(kernel, 0, sizeof(cl_mem), (void *)&c_memobj));
  size_t gsz[1] = {P}, lsz[1] = {1};
  CL_CHECK(clEnqueueNDRangeKernel(commandQueue, kernel, 1, NULL, gsz, lsz, 0, NULL, NULL));
  CL_CHECK(clFinish(commandQueue));
  CL_CHECK(clEnqueueReadBuffer(commandQueue, c_memobj, CL_TRUE, 0, out.size() * 4, out.data(), 0, NULL, NULL));

  std::cout << "# g core warp lane tmask" << std::endl;
  for (uint32_t g = 0; g < P; ++g) {
    std::printf("%u %d %d %d 0x%x\n", g, out[4*g], out[4*g+1], out[4*g+2], (unsigned)out[4*g+3]);
  }
  if (!use_csr || W == 0) {
    std::cout << "# no CSRs (or W = 0): mapping not checked" << std::endl;
    return 0;
  }
  int bad = 0;
  for (uint32_t g = 0; g < P; ++g) {
    uint32_t first = g - g % W;
    bool lane_ok = out[4*g+2] == (int)(g % W);
    bool same_warp = out[4*g] == out[4*first] && out[4*g+1] == out[4*first+1];
    bool full = (uint32_t)out[4*g+3] == ((1u << W) - 1);
    if (!(lane_ok && same_warp && full)) {
      if (bad < 20) std::printf("# MISMATCH g=%u: lane %d (expected %u)%s%s\n", g, out[4*g+2], g % W,
                                same_warp ? "" : ", not in the same warp as g-g%W",
                                full ? "" : ", tmask not full");
      ++bad;
    }
  }
  std::cout << (bad ? "# MAPPING MISMATCH: " + std::to_string(bad) + " work-items"
                    : std::string("# MAPPING OK: work-item p runs on lane p % W, full warps")) << std::endl;
  return bad ? 1 : 0;
}

int main (int argc, char **argv) {
  register_signal_handlers();
  parse_args(argc, argv);

  std::srand(50);

  const uint32_t a_points = M * K;
  const uint32_t b_points = K * N;
  const uint32_t c_points = M * N;

  // fixed data appended to A and B, check slots appended to C
  const uint32_t a_fix = a_points;                 // K values + padding
  const uint32_t b_fix = b_points;                 // K rows of N values + one padding row
  const uint32_t c_chk = c_points;
  std::vector<uint32_t> all_elems(c_points);
  for (uint32_t i = 0; i < c_points; ++i) all_elems[i] = i;
  Geometry geo = build_geometry(all_elems, false, a_fix, b_fix, c_chk);

  const uint32_t a_total = a_points + K + 16;
  const uint32_t b_total = b_points + (K + 1) * N;
  const uint32_t c_total = c_points + geo.S;

  cl_platform_id platform_id;
  CL_CHECK(clGetPlatformIDs(1, &platform_id, NULL));
  CL_CHECK(clGetDeviceIDs(platform_id, CL_DEVICE_TYPE_DEFAULT, 1, &device_id, NULL));
  context = CL_CHECK2(clCreateContext(NULL, 1, &device_id, NULL, NULL, &_err));
  commandQueue = CL_CHECK2(clCreateCommandQueue(context, device_id, 0, &_err));

  load_program(use_csr ? "-DVX_CSR" : "");

  if (probe_mode) {
    int rc = run_probe(geo.P);
    cleanup();
    return rc;
  }

  const std::string kname = variant == 0 ? std::string("sgemm_sent") : "sgemm_sent_v" + std::to_string(variant);
  kernel = CL_CHECK2(clCreateKernel(program, kname.c_str(), &_err));

  // ---- host data ----
  std::vector<float> h_a(a_total, FIX_PAD);
  std::vector<float> h_b(b_total, FIX_PAD);
  for (uint32_t i = 0; i < a_points; ++i) h_a[i] = static_cast<float>(rand()) / RAND_MAX;
  for (uint32_t i = 0; i < b_points; ++i) h_b[i] = static_cast<float>(rand()) / RAND_MAX;
  // sentinel operands: small distinct integers -> exact result whatever the
  // summation order or fma contraction; a skipped/repeated iteration or a
  // wrong stride changes the sum
  int64_t expected_i = 0;
  for (int k = 0; k < K; ++k) {
    h_a[a_fix + k] = (float)(k + 1);
    h_b[b_fix + (size_t)k * N] = (float)(K - k);
    expected_i += (int64_t)(k + 1) * (K - k);
  }
  const float expected = (float)expected_i;      // exact while < 2^24 (K <= ~410)

  std::vector<float> h_c(c_total, bitsf(POISON_BITS));
  std::vector<int32_t> h_t(c_total, 0);

  const size_t a_nbytes = a_total * sizeof(float);
  const size_t b_nbytes = b_total * sizeof(float);
  const size_t c_nbytes = c_total * sizeof(float);
  const size_t t_nbytes = c_total * sizeof(int32_t);
  const size_t o_nbytes = geo.P * sizeof(int32_t);

  a_memobj  = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_ONLY,  a_nbytes, NULL, &_err));
  b_memobj  = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_ONLY,  b_nbytes, NULL, &_err));
  c_memobj  = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_WRITE, c_nbytes, NULL, &_err));
  t_memobj  = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_WRITE, t_nbytes, NULL, &_err));
  oa_memobj = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_ONLY,  o_nbytes, NULL, &_err));
  ob_memobj = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_ONLY,  o_nbytes, NULL, &_err));
  oc_memobj = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_ONLY,  o_nbytes, NULL, &_err));

  CL_CHECK(clSetKernelArg(kernel, 0, sizeof(cl_mem), (void *)&c_memobj));
  CL_CHECK(clSetKernelArg(kernel, 1, sizeof(cl_mem), (void *)&t_memobj));
  CL_CHECK(clSetKernelArg(kernel, 2, sizeof(cl_mem), (void *)&a_memobj));
  CL_CHECK(clSetKernelArg(kernel, 3, sizeof(cl_mem), (void *)&b_memobj));
  CL_CHECK(clSetKernelArg(kernel, 4, sizeof(cl_mem), (void *)&oa_memobj));
  CL_CHECK(clSetKernelArg(kernel, 5, sizeof(cl_mem), (void *)&ob_memobj));
  CL_CHECK(clSetKernelArg(kernel, 6, sizeof(cl_mem), (void *)&oc_memobj));
  CL_CHECK(clSetKernelArg(kernel, 7, sizeof(int32_t), &N));
  CL_CHECK(clSetKernelArg(kernel, 8, sizeof(int32_t), &K));

  CL_CHECK(clEnqueueWriteBuffer(commandQueue, a_memobj,  CL_TRUE, 0, a_nbytes, h_a.data(), 0, NULL, NULL));
  CL_CHECK(clEnqueueWriteBuffer(commandQueue, b_memobj,  CL_TRUE, 0, b_nbytes, h_b.data(), 0, NULL, NULL));
  CL_CHECK(clEnqueueWriteBuffer(commandQueue, c_memobj,  CL_TRUE, 0, c_nbytes, h_c.data(), 0, NULL, NULL));
  CL_CHECK(clEnqueueWriteBuffer(commandQueue, t_memobj,  CL_TRUE, 0, t_nbytes, h_t.data(), 0, NULL, NULL));
  CL_CHECK(clEnqueueWriteBuffer(commandQueue, oa_memobj, CL_TRUE, 0, o_nbytes, geo.offA.data(), 0, NULL, NULL));
  CL_CHECK(clEnqueueWriteBuffer(commandQueue, ob_memobj, CL_TRUE, 0, o_nbytes, geo.offB.data(), 0, NULL, NULL));
  CL_CHECK(clEnqueueWriteBuffer(commandQueue, oc_memobj, CL_TRUE, 0, o_nbytes, geo.offC.data(), 0, NULL, NULL));

  size_t global_size[1] = {geo.P};
  size_t local_size[1] = {1};
  CL_CHECK(clEnqueueNDRangeKernel(commandQueue, kernel, 1, NULL, global_size, local_size, 0, NULL, NULL));
  CL_CHECK(clFinish(commandQueue));

  CL_CHECK(clEnqueueReadBuffer(commandQueue, c_memobj, CL_TRUE, 0, c_nbytes, h_c.data(), 0, NULL, NULL));
  CL_CHECK(clEnqueueReadBuffer(commandQueue, t_memobj, CL_TRUE, 0, t_nbytes, h_t.data(), 0, NULL, NULL));

  // ---- real results ----
  std::vector<float> ref_vec(c_points);
  matmul_cpu(ref_vec.data(), h_a.data(), h_b.data(), M, N, K);

  auto compare = [&](std::vector<Divergence>& out) {
    out.clear();
    for (uint32_t i = 0; i < c_points; ++i) {
      auto ref = ref_vec[i];
      auto cur = h_c[i];
      if (!compare_equal(cur, ref)) out.push_back({i, cur, ref, calculate_hamming_distance(cur, ref)});
    }
  };

  // ---- sentinel checks: per slot, and the set of warps that fired ----
  const int32_t expected_mask = use_csr ? (int32_t)((W > 0 ? (1u << W) : 0u) - 1) : -1;
  // returns the warps (of geometry g) with at least one failed slot; counts by kind
  auto check = [&](const Geometry& g, const std::vector<float>& c, const std::vector<int32_t>& t, uint32_t chk,
                   uint32_t& n_value, uint32_t& n_missing, uint32_t& n_tmask) {
    std::vector<bool> fired(W > 0 ? g.P / W : 0, false);
    n_value = n_missing = n_tmask = 0;
    for (uint32_t s = 0; s < g.S; ++s) {
      uint32_t v = fbits(c[chk + s]);
      bool bad = false;
      if (v == POISON_BITS) { ++n_missing; bad = true; }        // sentinel never stored: warp lost
      else {
        if (v != fbits(expected)) { ++n_value; bad = true; }    // wrong instruction stream
        if (t[chk + s] != expected_mask) { ++n_tmask; bad = true; }  // lanes missing from the warp
      }
      if (bad) fired[g.slot_warp[s]] = true;
    }
    return fired;
  };

  uint32_t bad_value = 0, missing = 0, bad_tmask = 0;
  std::vector<bool> fired = check(geo, h_c, h_t, c_chk, bad_value, missing, bad_tmask);
  const bool alarm = (bad_value + missing + bad_tmask) > 0;

  std::vector<Divergence> divergences;
  compare(divergences);
  const bool passed_before = divergences.empty();
  const size_t div_before = divergences.size();
  uint32_t lanes_before = 0;
  if (W > 1) {
    std::vector<bool> lane_hit(W - 1, false);
    for (auto& d : divergences) lane_hit[d.index % (W - 1)] = true;
    for (bool b : lane_hit) lanes_before += b;
  }

  // ---- recovery: relaunch only the elements of the warps whose sentinel fired ----
  int tries = 0;
  uint32_t groups = 0, rec_elements = 0;
  std::vector<uint32_t> todo;                       // elements of C still to recompute
  if (recovery > 0 && W > 0 && alarm) {
    for (uint32_t w = 0; w < fired.size(); ++w) {
      if (!fired[w] || geo.warp_items[w].empty()) continue;
      ++groups;
      for (uint32_t j : geo.warp_items[w]) todo.push_back(all_elems[j]);
    }
    rec_elements = todo.size();
  }
  while (!todo.empty() && tries < recovery) {
    ++tries;
    const uint32_t n = todo.size();
    Geometry g2 = build_geometry(todo, true, a_fix, b_fix, n);
    const uint32_t c2_total = n + g2.S;
    std::vector<float> c2(c2_total, bitsf(POISON_BITS));
    std::vector<int32_t> t2(c2_total, 0);
    const size_t o2 = g2.P * sizeof(int32_t);
    cl_mem c2m  = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_WRITE, c2_total * 4, NULL, &_err));
    cl_mem t2m  = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_WRITE, c2_total * 4, NULL, &_err));
    cl_mem oa2m = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_ONLY, o2, NULL, &_err));
    cl_mem ob2m = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_ONLY, o2, NULL, &_err));
    cl_mem oc2m = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_ONLY, o2, NULL, &_err));
    CL_CHECK(clEnqueueWriteBuffer(commandQueue, c2m,  CL_TRUE, 0, c2_total * 4, c2.data(), 0, NULL, NULL));
    CL_CHECK(clEnqueueWriteBuffer(commandQueue, t2m,  CL_TRUE, 0, c2_total * 4, t2.data(), 0, NULL, NULL));
    CL_CHECK(clEnqueueWriteBuffer(commandQueue, oa2m, CL_TRUE, 0, o2, g2.offA.data(), 0, NULL, NULL));
    CL_CHECK(clEnqueueWriteBuffer(commandQueue, ob2m, CL_TRUE, 0, o2, g2.offB.data(), 0, NULL, NULL));
    CL_CHECK(clEnqueueWriteBuffer(commandQueue, oc2m, CL_TRUE, 0, o2, g2.offC.data(), 0, NULL, NULL));
    CL_CHECK(clSetKernelArg(kernel, 0, sizeof(cl_mem), (void *)&c2m));
    CL_CHECK(clSetKernelArg(kernel, 1, sizeof(cl_mem), (void *)&t2m));
    CL_CHECK(clSetKernelArg(kernel, 4, sizeof(cl_mem), (void *)&oa2m));
    CL_CHECK(clSetKernelArg(kernel, 5, sizeof(cl_mem), (void *)&ob2m));
    CL_CHECK(clSetKernelArg(kernel, 6, sizeof(cl_mem), (void *)&oc2m));
    size_t gs2[1] = {g2.P};
    CL_CHECK(clEnqueueNDRangeKernel(commandQueue, kernel, 1, NULL, gs2, local_size, 0, NULL, NULL));
    CL_CHECK(clFinish(commandQueue));
    CL_CHECK(clEnqueueReadBuffer(commandQueue, c2m, CL_TRUE, 0, c2_total * 4, c2.data(), 0, NULL, NULL));
    CL_CHECK(clEnqueueReadBuffer(commandQueue, t2m, CL_TRUE, 0, c2_total * 4, t2.data(), 0, NULL, NULL));
    cl_mem tmp[] = {c2m, t2m, oa2m, ob2m, oc2m};
    for (cl_mem m : tmp) clReleaseMemObject(m);

    uint32_t v2, m2, k2;
    std::vector<bool> fired2 = check(g2, c2, t2, n, v2, m2, k2);
    std::vector<uint32_t> again;
    for (uint32_t w = 0; w < fired2.size(); ++w) {
      for (uint32_t j : g2.warp_items[w]) {
        if (fired2[w]) again.push_back(todo[j]);    // still suspicious: next try
        else h_c[todo[j]] = c2[j];                  // verified by its sentinel: accept
      }
    }
    todo.swap(again);
  }
  const bool unresolved = !todo.empty();
  if (tries > 0) compare(divergences);

  cleanup();

  bool passed = divergences.empty();
  std::cout << "{\n";
  std::cout << "  \"status\": \"" << (passed ? "masked_error" : "sdc") << "\",\n";
  std::cout << "  \"total_points\": " << c_points << ",\n";
  std::cout << "  \"divergent_count\": " << divergences.size() << ",\n";
  std::cout << "  \"sentinel_alarm\": " << (alarm ? "true" : "false") << ",\n";
  std::cout << "  \"sentinel\": {\"variant\": " << variant << ", \"warp_width\": " << W << ", \"work_items\": " << geo.P
            << ", \"slots\": " << geo.S << ", \"bad_value\": " << bad_value
            << ", \"missing\": " << missing << ", \"bad_tmask\": " << bad_tmask << "},\n";
  std::cout << "  \"recovery\": {\"max_tries\": " << recovery << ", \"tries\": " << tries
            << ", \"groups\": " << groups << ", \"elements\": " << rec_elements
            << ", \"unresolved\": " << (unresolved ? "true" : "false")
            << ", \"status_before\": \"" << (passed_before ? "masked_error" : "sdc") << "\""
            << ", \"divergent_before\": " << div_before << ", \"lanes_before\": " << lanes_before << "},\n";
  std::cout << "  \"divergent_data\": [\n";

  for (size_t i = 0; i < divergences.size(); ++i) {
    uint32_t actual_bits, expected_bits;
    std::memcpy(&actual_bits, &divergences[i].actual, sizeof(float));
    std::memcpy(&expected_bits, &divergences[i].expected, sizeof(float));

    char actual_hex[11];
    char expected_hex[11];
    std::snprintf(actual_hex, sizeof(actual_hex), "0x%08x", actual_bits);
    std::snprintf(expected_hex, sizeof(expected_hex), "0x%08x", expected_bits);

    std::cout << "    {\n";
    std::cout << "      \"index_value\": " << divergences[i].index << ",\n";
    std::cout << "      \"actual_v\": \"" << actual_hex << "\",\n";
    std::cout << "      \"expected_v\": \"" << expected_hex << "\",\n";
    std::cout << "      \"hamming_dist\": " << divergences[i].hamming_distance << "\n";
    std::cout << "    }" << (i + 1 < divergences.size() ? "," : "") << "\n";
  }

  std::cout << "  ]\n";
  std::cout << "}\n";

  return passed ? 0 : 1;
}
