#include <iostream>
#include <fstream>
#include <unistd.h>
#include <string.h>
#include <vector>
#include <vortex.h>
#include <cmath>
#include "common.h"

#define FLOAT_ULP 6

// File di Log Statistici
const char* log_sdc_file   = "stats_sdc.log";
const char* log_crash_file = "stats_crash.log";

// Macro RT_CHECK modificata: non fa più exit, scrive sul log di Crash e fa cleanup ordinato
#define RT_CHECK(_expr)                                                     \
   do {                                                                     \
     int _ret = _expr;                                                      \
     if (0 == _ret)                                                         \
       break;                                                               \
     std::ofstream clog(log_crash_file, std::ios::app);                     \
     if (clog.is_open()) {                                                  \
       clog << "[CRASH] API Error: '" << #_expr << "' returned " << _ret     \
            << " | Arguments Size: " << size << std::endl;                  \
     }                                                                      \
     cleanup();                                                             \
     return -2; /* Ritorniamo un codice specifico per identificare il Crash */ \
   } while (false)

///////////////////////////////////////////////////////////////////////////////
#include <cstring>

// 1. Dichiarazione del Template Base (Obbligatoria prima delle specializzazioni)
template <typename Type>
class Comparator; // Basta la forward declaration se specializzi tutto, o class Comparator {};

// 2. Funzione helper per la Hamming Distance (messa inline fuori dal template)
inline int calculate_hamming(uint32_t a, uint32_t b) {
    uint32_t xor_result = a ^ b;
    #if defined(__GNUC__) || defined(__clang__)
        return __builtin_popcount(xor_result);
    #else
        int count = 0;
        while (xor_result) {
            count += xor_result & 1;
            xor_result >>= 1;
        }
        return count;
    #endif
}

// 3. Unica specializzazione per float indurita con Hamming Distance
template <>
class Comparator<float> {
public:
  static const char* type_str() { return "float"; }
  
  // Ripristiniamo il generatore originale di Vortex che serviva al test
  static float generate() { return static_cast<float>(rand()) / RAND_MAX; }
  
  static bool compare(float a, float b, int index, std::ofstream& sdclog) {
    // Manteniamo anche il calcolo ULP originale per sicurezza/soglia
    union fi_t { float f; int32_t i; };
    fi_t fa, fb;
    fa.f = a;
    fb.f = b;
    auto ulp_diff = std::abs(fa.i - fb.i);

    // Se superiamo la tolleranza ULP (FLOAT_ULP è definita da qualche parte sopra nel file)
    if (ulp_diff > FLOAT_ULP) {
      if (sdclog.is_open()) {
        // Estraiamo i bit raw IEEE 754 per la Hamming Distance
        uint32_t a_bits, b_bits;
        std::memcpy(&a_bits, &a, sizeof(float));
        std::memcpy(&b_bits, &b, sizeof(float));

        int hamming_dist = calculate_hamming(a_bits, b_bits);

        // Salviamo i flag del log
        std::ios_base::fmtflags f(sdclog.flags());
        
        // Output ultra-dettagliato: Valore umano, bit esadecimali, ULP e Hamming!
        sdclog << "  Index [" << index << "] -> "
               << "Expected: " << b << " (0x" << std::hex << b_bits << "), "
               << "Actual: " << a << " (0x" << std::hex << a_bits << ") | "
               << "ULP Diff: " << std::dec << ulp_diff << " | "
               << "Hamming Dist: " << hamming_dist << "\n";

        // Ripristiniamo i flag
        sdclog.flags(f);
      }
      return false;
    }
    return true;
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

static void show_usage() {
   std::cout << "Vortex Test Parallel Statistics Mode." << std::endl;
   std::cout << "Usage: [-k: kernel] [-n words] [-h: help]" << std::endl;
}

static void parse_args(int argc, char **argv) {
  int c;
  while ((c = getopt(argc, argv, "n:k:h")) != -1) {
    switch (c) {
    case 'n': size = atoi(optarg); break;
    case 'k': kernel_file = optarg; break;
    case 'h': show_usage(); exit(0); break;
    default:  show_usage(); exit(-1);
    }
  }
}

void cleanup() {
  if (device) {
    if (src0_buffer) vx_mem_free(src0_buffer);
    if (src1_buffer) vx_mem_free(src1_buffer);
    if (dst_buffer)  vx_mem_free(dst_buffer);
    if (krnl_buffer) vx_mem_free(krnl_buffer);
    if (args_buffer) vx_mem_free(args_buffer);
    vx_dev_close(device);
  }
}

int main(int argc, char *argv[]) {
  parse_args(argc, argv);

  // Usiamo un seed dinamico o basato sul tempo se lo script lancia esecuzioni consecutive,
  // altrimenti con std::srand(50) i dati di input saranno identici ad ogni run.
  // std::srand(time(NULL)); 
  std::srand(50);

  // Rimossi tutti i log a terminale standard (std::cout) per rendere le esecuzioni silenziose
  RT_CHECK(vx_dev_open(&device));

  uint32_t num_points = size;
  uint32_t buf_size = num_points * sizeof(TYPE);

  kernel_arg.num_points = num_points;

  // Allocazione memoria ed handshake driver
  RT_CHECK(vx_mem_alloc(device, buf_size, VX_MEM_READ, &src0_buffer));
  RT_CHECK(vx_mem_address(src0_buffer, &kernel_arg.src0_addr));
  RT_CHECK(vx_mem_alloc(device, buf_size, VX_MEM_READ, &src1_buffer));
  RT_CHECK(vx_mem_address(src1_buffer, &kernel_arg.src1_addr));
  RT_CHECK(vx_mem_alloc(device, buf_size, VX_MEM_WRITE, &dst_buffer));
  RT_CHECK(vx_mem_address(dst_buffer, &kernel_arg.dst_addr));

  std::vector<TYPE> h_src0(num_points);
  std::vector<TYPE> h_src1(num_points);
  std::vector<TYPE> h_dst(num_points);

  for (uint32_t i = 0; i < num_points; ++i) {
    h_src0[i] = Comparator<TYPE>::generate();
    h_src1[i] = Comparator<TYPE>::generate();
  }

  RT_CHECK(vx_copy_to_dev(src0_buffer, h_src0.data(), 0, buf_size));
  RT_CHECK(vx_copy_to_dev(src1_buffer, h_src1.data(), 0, buf_size));
  RT_CHECK(vx_upload_kernel_file(device, kernel_file, &krnl_buffer));
  RT_CHECK(vx_upload_bytes(device, &kernel_arg, sizeof(kernel_arg_t), &args_buffer));

  // Avvio esecuzione hardware/simulatore
  RT_CHECK(vx_start(device, krnl_buffer, args_buffer));

  // Il timeout restituisce errore se la GPU si pianta (intercettato da RT_CHECK come CRASH)
  RT_CHECK(vx_ready_wait(device, VX_MAX_TIMEOUT));

  RT_CHECK(vx_copy_from_dev(h_dst.data(), dst_buffer, 0, buf_size));

  // Verifica dei Risultati (Identificazione SDC)
  int errors = 0;
  std::ofstream sdclog;

  for (uint32_t i = 0; i < num_points; ++i) {
    auto ref = h_src0[i] + h_src1[i];
    auto cur = h_dst[i];
    
    // Apriamo il file di log solo al primo errore riscontrato per risparmiare I/O
    if (!sdclog.is_open() && !Comparator<TYPE>::compare(cur, ref, i, sdclog)) {
      sdclog.open(log_sdc_file, std::ios::app);
      sdclog << "[SDC DETECTED] Run metrics: points=" << num_points << "\n";
      // Chiamata ripetuta passandogli il file aperto per stampare l'errore corrente
      Comparator<TYPE>::compare(cur, ref, i, sdclog);
      ++errors;
    } else if (sdclog.is_open() && !Comparator<TYPE>::compare(cur, ref, i, sdclog)) {
      ++errors;
    }
  }

  if (sdclog.is_open()) {
    sdclog << "  Total errors in this run: " << errors << "\n----------------------------------------\n";
    sdclog.close();
  }

  cleanup();

  // Ritorni differenziati per lo script di automazione statistica
  if (errors != 0) {
    return 1; // 1 = Esecuzione completata ma con Silent Data Corruption (SDC)
  }

  return 0; // 0 = PASSED (Tutto perfettamente sano)
}