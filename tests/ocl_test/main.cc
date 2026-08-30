#include <stdio.h>
#include <stdlib.h>
#include <assert.h>
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
#define KERNEL_NAME "sgemm"

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
uint8_t* kernel_bin = NULL;

void cleanup() {
  if (commandQueue) { clReleaseCommandQueue(commandQueue); commandQueue = NULL; }
  if (kernel) { clReleaseKernel(kernel); kernel = NULL; }
  if (program) { clReleaseProgram(program); program = NULL; }
  if (a_memobj) { clReleaseMemObject(a_memobj); a_memobj = NULL; }
  if (b_memobj) { clReleaseMemObject(b_memobj); b_memobj = NULL; }
  if (c_memobj) { clReleaseMemObject(c_memobj); c_memobj = NULL; }
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

static void parse_args(int argc, char **argv) {
  int c;
  while ((c = getopt(argc, argv, "m:n:k:h")) != -1) {
    switch (c) {
    case 'm':
      M = atoi(optarg);
      break;
    case 'n':
      N = atoi(optarg);
      break;
    case 'k':
      K = atoi(optarg);
      break;
    case 'h':
    default:
      output_crash_json(nullptr, -1, "Invalid command-line arguments");
      exit(-1);
    }
  }
}

int main (int argc, char **argv) {
  // Registra i gestori di segnale per catturare crash hardware/OS
  register_signal_handlers();

  // Parse degli argomenti
  parse_args(argc, argv);

  std::srand(50);

  uint32_t a_points = M * K;
  uint32_t b_points = K * N;
  uint32_t c_points = M * N;

  cl_platform_id platform_id;
  size_t kernel_size;

  // Inizializzazione OpenCL
  CL_CHECK(clGetPlatformIDs(1, &platform_id, NULL));
  CL_CHECK(clGetDeviceIDs(platform_id, CL_DEVICE_TYPE_DEFAULT, 1, &device_id, NULL));

  context = CL_CHECK2(clCreateContext(NULL, 1, &device_id, NULL, NULL, &_err));

  size_t a_nbytes = a_points * sizeof(float);
  size_t b_nbytes = b_points * sizeof(float);
  size_t c_nbytes = c_points * sizeof(float);

  a_memobj = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_ONLY, a_nbytes, NULL, &_err));
  b_memobj = CL_CHECK2(clCreateBuffer(context, CL_MEM_READ_ONLY, b_nbytes, NULL, &_err));
  c_memobj = CL_CHECK2(clCreateBuffer(context, CL_MEM_WRITE_ONLY, c_nbytes, NULL, &_err));

  if (0 != read_kernel_file("kernel.cl", &kernel_bin, &kernel_size)) {
    cleanup();
    output_crash_json("read_kernel_file", -1, "Failed to load kernel file");
    exit(-1);
  }

  program = CL_CHECK2(clCreateProgramWithSource(
    context, 1, (const char**)&kernel_bin, &kernel_size, &_err));

  CL_CHECK(clBuildProgram(program, 1, &device_id, NULL, NULL, NULL));
  kernel = CL_CHECK2(clCreateKernel(program, KERNEL_NAME, &_err));

  size_t global_size[2] = {(size_t)N, (size_t)M};
  size_t local_size[2] = {1, 1};

  CL_CHECK(clSetKernelArg(kernel, 0, sizeof(cl_mem), (void *)&c_memobj));
  CL_CHECK(clSetKernelArg(kernel, 1, sizeof(cl_mem), (void *)&a_memobj));
  CL_CHECK(clSetKernelArg(kernel, 2, sizeof(cl_mem), (void *)&b_memobj));
  CL_CHECK(clSetKernelArg(kernel, 3, sizeof(uint32_t), &M));
  CL_CHECK(clSetKernelArg(kernel, 4, sizeof(uint32_t), &N));
  CL_CHECK(clSetKernelArg(kernel, 5, sizeof(uint32_t), &K));

  std::vector<float> h_a(a_points);
  std::vector<float> h_b(b_points);
  std::vector<float> h_c(c_points, 0.0f);

  for (uint32_t i = 0; i < a_points; ++i) {
    h_a[i] = static_cast<float>(rand()) / RAND_MAX;
  }
  for (uint32_t i = 0; i < b_points; ++i) {
    h_b[i] = static_cast<float>(rand()) / RAND_MAX;
  }

  commandQueue = CL_CHECK2(clCreateCommandQueue(context, device_id, 0, &_err));

  CL_CHECK(clEnqueueWriteBuffer(commandQueue, a_memobj, CL_TRUE, 0, a_nbytes, h_a.data(), 0, NULL, NULL));
  CL_CHECK(clEnqueueWriteBuffer(commandQueue, b_memobj, CL_TRUE, 0, b_nbytes, h_b.data(), 0, NULL, NULL));

  CL_CHECK(clEnqueueNDRangeKernel(commandQueue, kernel, 2, NULL, global_size, local_size, 0, NULL, NULL));
  CL_CHECK(clFinish(commandQueue));

  CL_CHECK(clEnqueueReadBuffer(commandQueue, c_memobj, CL_TRUE, 0, c_nbytes, h_c.data(), 0, NULL, NULL));

  // Verifiche e tracciamento delle divergenze
  std::vector<float> ref_vec(c_points);
  matmul_cpu(ref_vec.data(), h_a.data(), h_b.data(), M, N, K);

  std::vector<Divergence> divergences;
  for (uint32_t i = 0; i < c_points; ++i) {
    auto ref = ref_vec[i];
    auto cur = h_c[i];

    if (!compare_equal(cur, ref)) {
      int dist = calculate_hamming_distance(cur, ref);
      divergences.push_back({i, cur, ref, dist});
    }
  }

  cleanup();

  // Standard Output (Pass / SDC)
  bool passed = divergences.empty();
  std::cout << "{\n";
  std::cout << "  \"status\": \"" << (passed ? "masked_error" : "sdc") << "\",\n";
  std::cout << "  \"total_points\": " << c_points << ",\n";
  std::cout << "  \"divergent_count\": " << divergences.size() << ",\n";
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



