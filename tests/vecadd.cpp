#include <iostream>
#include <unistd.h>
#include <cstring>
#include <vector>
#include <cmath>
#include <cstdint>
#include <csignal>
#include <vortex.h>
#include "common.h"

#define FLOAT_ULP 6

// Helper to output formatted JSON on crashes or hardware errors
void output_crash_json(const char* expr, int code, const char* details) {
  std::cout << "{\n";
  std::cout << "  \"status\": \"crash\",\n";
  std::cout << "  \"error\": {\n";
  if (expr) {
    std::cout << "    \"expression\": \"" << expr << "\",\n";
  }
  std::cout << "    \"code\": " << code << ",\n";
  std::cout << "    \"details\": \"" << (details ? details : "Runtime execution error") << "\"\n";
  std::cout << "  }\n";
  std::cout << "}\n";
}

void cleanup(); // Forward declaration

// Signal handler to catch segmentation faults, aborts, etc.
void signal_handler(int sig) {
  cleanup();
  output_crash_json(nullptr, sig, strsignal(sig));
  _exit(sig);
}

#define RT_CHECK(_expr)                                                     \
   do {                                                                     \
     int _ret = _expr;                                                      \
     if (0 == _ret)                                                         \
       break;                                                               \
     cleanup();                                                             \
     output_crash_json(#_expr, _ret, "Vortex runtime API call failed");     \
     exit(-1);                                                              \
   } while (false)

///////////////////////////////////////////////////////////////////////////////

struct Divergence {
  uint32_t index;
  TYPE actual;
  TYPE expected;
  int hamming_distance;
};

// Calculate bitwise Hamming distance between two values
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

template <typename Type>
class Comparator {};

template <>
class Comparator<int> {
public:
  static int generate() {
    return rand();
  }
  static bool compare(int a, int b) {
    return (a == b);
  }
};

template <>
class Comparator<float> {
public:
  static float generate() {
    return static_cast<float>(rand()) / RAND_MAX;
  }
  static bool compare(float a, float b) {
    union fi_t { float f; int32_t i; };
    fi_t fa, fb;
    fa.f = a;
    fb.f = b;
    auto d = std::abs(fa.i - fb.i);
    return (d <= FLOAT_ULP);
  }
};

const char* kernel_file = "kernel.vxbin";
uint32_t size = 16;

vx_device_h device = nullptr;
vx_buffer_h src0_buffer = nullptr;
vx_buffer_h src1_buffer = nullptr;
vx_buffer_h dst_buffer = nullptr;
vx_buffer_h krnl_buffer = nullptr;
vx_buffer_h args_buffer = nullptr;
kernel_arg_t kernel_arg = {};

static void parse_args(int argc, char **argv) {
  int c;
  while ((c = getopt(argc, argv, "n:k:h")) != -1) {
    switch (c) {
    case 'n':
      size = atoi(optarg);
      break;
    case 'k':
      kernel_file = optarg;
      break;
    case 'h':
    default:
      output_crash_json(nullptr, -1, "Invalid command-line arguments");
      exit(-1);
    }
  }
}

void cleanup() {
  if (device) {
    vx_mem_free(src0_buffer);
    vx_mem_free(src1_buffer);
    vx_mem_free(dst_buffer);
    vx_mem_free(krnl_buffer);
    vx_mem_free(args_buffer);
    vx_dev_close(device);
    device = nullptr;
  }
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

int main(int argc, char *argv[]) {
  // Catch fatal signals to format system crashes as JSON
  register_signal_handlers();

  // Parse command line arguments
  parse_args(argc, argv);

  std::srand(50);

  // Open device connection
  RT_CHECK(vx_dev_open(&device));

  uint32_t num_points = size;
  uint32_t buf_size = num_points * sizeof(TYPE);

  kernel_arg.num_points = num_points;

  // Allocate device memory
  RT_CHECK(vx_mem_alloc(device, buf_size, VX_MEM_READ, &src0_buffer));
  RT_CHECK(vx_mem_address(src0_buffer, &kernel_arg.src0_addr));
  RT_CHECK(vx_mem_alloc(device, buf_size, VX_MEM_READ, &src1_buffer));
  RT_CHECK(vx_mem_address(src1_buffer, &kernel_arg.src1_addr));
  RT_CHECK(vx_mem_alloc(device, buf_size, VX_MEM_WRITE, &dst_buffer));
  RT_CHECK(vx_mem_address(dst_buffer, &kernel_arg.dst_addr));

  // Allocate host buffers
  std::vector<TYPE> h_src0(num_points);
  std::vector<TYPE> h_src1(num_points);
  std::vector<TYPE> h_dst(num_points);

  for (uint32_t i = 0; i < num_points; ++i) {
    h_src0[i] = Comparator<TYPE>::generate();
    h_src1[i] = Comparator<TYPE>::generate();
  }

  // Upload source buffers and kernel binary
  RT_CHECK(vx_copy_to_dev(src0_buffer, h_src0.data(), 0, buf_size));
  RT_CHECK(vx_copy_to_dev(src1_buffer, h_src1.data(), 0, buf_size));
  RT_CHECK(vx_upload_kernel_file(device, kernel_file, &krnl_buffer));
  RT_CHECK(vx_upload_bytes(device, &kernel_arg, sizeof(kernel_arg_t), &args_buffer));

  // Start execution & wait
  RT_CHECK(vx_start(device, krnl_buffer, args_buffer));
  RT_CHECK(vx_ready_wait(device, VX_MAX_TIMEOUT));

  // Download destination buffer
  RT_CHECK(vx_copy_from_dev(h_dst.data(), dst_buffer, 0, buf_size));

  // Verification & Divergence tracking
  std::vector<Divergence> divergences;
  for (uint32_t i = 0; i < num_points; ++i) {
    auto ref = h_src0[i] + h_src1[i];
    auto cur = h_dst[i];

    if (!Comparator<TYPE>::compare(cur, ref)) {
      int dist = calculate_hamming_distance(cur, ref);
      divergences.push_back({i, cur, ref, dist});
    }
  }

  // Cleanup hardware allocation
  cleanup();

  // Standard Output (Pass / SDC)
  bool passed = divergences.empty();
  std::cout << "{\n";
  std::cout << "  \"status\": \"" << (passed ? "masked_error" : "sdc") << "\",\n";
  std::cout << "  \"total_points\": " << num_points << ",\n";
  std::cout << "  \"divergent_count\": " << divergences.size() << ",\n";
  std::cout << "  \"divergent_data\": [\n";

  for (size_t i = 0; i < divergences.size(); ++i) {
    std::cout << "    {\n";
    std::cout << "      \"index\": " << divergences[i].index << ",\n";
    std::cout << "      \"actual_value\": " << divergences[i].actual << ",\n";
    std::cout << "      \"expected_value\": " << divergences[i].expected << ",\n";
    std::cout << "      \"hamming_distance\": " << divergences[i].hamming_distance << "\n";
    std::cout << "    }" << (i + 1 < divergences.size() ? "," : "") << "\n";
  }

  std::cout << "  ]\n";
  std::cout << "}\n";

  return passed ? 0 : 1;
}