#ifndef FAULT_INJECTION_H
#define FAULT_INJECTION_H

#include <cstdint>
#include <cstddef>

#include "Vrtlsim_shim___024root.h"

class Vrtlsim_shim___024root;

enum VLDataType { 
    CDATA_8,
    SDATA_16,
    IDATA_32,
    QDATA_64,
    WIDE_512
};

struct FaultTarget {
    std::size_t offset;                     // Memory offset of the first element
    std::uint32_t bit_reali_elem;           // Active bits inside a single primitive element
    std::uint32_t array_depth;              // VlUnpacked depth (T_Depth). 1 if scalar variable
    std::uint32_t consecutive_count;        // How many consecutive items are grouped here
    VLDataType type;                        // Verilator primitive data type (CDATA_8, etc.)
    std::uint32_t max_cumulato;             // Global probability ruler boundary
};


extern const uint32_t BIT_TOTALI_VORTEX; 
extern const FaultTarget FAULT_RULER[];
extern const size_t FAULT_RULER_SIZE;

void inject_random_fault(Vrtlsim_shim___024root* rootp);


static constexpr double lambda = 0.001;

uint64_t get_next_fault_time(uint64_t current_time);


#endif // FAULT_INJECTION_H