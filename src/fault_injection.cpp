#include "fault_injection.h"
#include <algorithm>
#include <iostream>
#include <random>

// Binary search comparator matching our probabilistic ruler boundaries
struct TargetComparator {
    bool operator()(const FaultTarget& target, uint32_t dart) const {
        return target.max_cumulato < dart;
    }
};

uint32_t inject_random_fault(Vrtlsim_shim___024root* rootp, uint32_t dart) {
    if (FAULT_RULER_SIZE == 0 || BIT_TOTALI_VORTEX == 0) {
        std::cerr << "[Fault Injection] Error: Fault ruler metadata is uninitialized or zero.\n";
        return 0xFFFFFFFF;
    }

    if (0xFFFFFFFF == dart)
    {
        // 1. Roll a single global uniform random dart across the total silicon area
        static std::mt19937 gen(std::random_device{}());
        std::uniform_int_distribution<uint32_t> dist(0, BIT_TOTALI_VORTEX - 1);
        dart = dist(gen);
    }

    // 2. O(log N) binary search to find the target structure hit by the dart
    const FaultTarget* it = std::lower_bound(
        FAULT_RULER, 
        FAULT_RULER + FAULT_RULER_SIZE, 
        dart, 
        TargetComparator{}
    );

    if (it == FAULT_RULER + FAULT_RULER_SIZE) {
        std::cerr << "[Fault Injection] Error: Random dart fell out of bounds.\n";
        return 0xFFFFFFFF;
    }

    // 3. Compute local block offset relative to the target's starting boundary
    uint32_t base_boundary = (it == FAULT_RULER) ? 0 : (it - 1)->max_cumulato;
    uint32_t local_offset = dart - base_boundary;

    // 4. Decode 2D geometry (Consecutive instances vs. Unpacked array rows)
    uint32_t bits_per_logical_instance = it->bit_reali_elem * it->array_depth;

    // Identify which consecutive variable/array copy was struck
    uint32_t consecutive_index = local_offset / bits_per_logical_instance;
    uint32_t instance_residual  = local_offset % bits_per_logical_instance;

    // Identify the internal array index for VlUnpacked elements (evaluates to 0 for scalars)
    uint32_t unpacked_row_index = instance_residual / it->bit_reali_elem;
    uint32_t target_bit_index   = instance_residual % it->bit_reali_elem;

    // 5. Compute base memory address of the structural block
    uint8_t* base_byte_ptr = reinterpret_cast<uint8_t*>(rootp) + it->offset;

    // 5b. Flat element index into the memory_grid: each consecutive instance
    //     occupies `array_depth` slots, so the stride between instances is
    //     array_depth, not 1. (Fixes silent mis-targeting when a target has
    //     both consecutive_index > 0 and unpacked_row_index > 0.)
    uint32_t flat_element_index = consecutive_index * it->array_depth + unpacked_row_index;

    // 6. Execute branchless memory manipulation based on native Verilator types
    // The combined stride accurately handles both individual scalars and complex arrays.
    switch (it->type) {
        case CDATA_8: {
            uint8_t* memory_grid = reinterpret_cast<uint8_t*>(base_byte_ptr);
            memory_grid[flat_element_index] ^= (1U << target_bit_index);
            break;
        }
        case SDATA_16: {
            uint16_t* memory_grid = reinterpret_cast<uint16_t*>(base_byte_ptr);
            memory_grid[flat_element_index] ^= (1U << target_bit_index);
            break;
        }
        case IDATA_32: {
            uint32_t* memory_grid = reinterpret_cast<uint32_t*>(base_byte_ptr);
            memory_grid[flat_element_index] ^= (1U << target_bit_index);
            break;
        }
        case QDATA_64: {
            uint64_t* memory_grid = reinterpret_cast<uint64_t*>(base_byte_ptr);
            memory_grid[flat_element_index] ^= (1ULL << target_bit_index);
            break;
        }
        case WIDE_512: {
            // Verilator structures wide signals (> 64 bits) as flat uint32_t subarrays.
            // We map down to the exact 32-bit subword window containing the target bit.
            uint32_t* memory_grid = reinterpret_cast<uint32_t*>(base_byte_ptr);

            uint32_t sub_word_32 = target_bit_index / 32;
            uint32_t bit_window  = target_bit_index % 32;

            // Fixed stride layout (512 bits / 32 bits = 16 words per discrete element)
            const uint32_t words_per_element = 16;

            uint32_t flat_wide_stride = flat_element_index * words_per_element + sub_word_32;

            memory_grid[flat_wide_stride] ^= (1U << bit_window);
            break;
        }
    }
    return dart;
}


uint64_t get_next_fault_time(uint64_t current_time) {
    static std::mt19937 gen{std::random_device{}()}; // Standard mersenne_twister_engine
    static std::uniform_real_distribution<double> dis{0.0, 1.0};

    double u = dis(gen);
    while (u >= 1.0) u = dis(gen); 
    
    // Exponential distribution formula
    double delta = -std::log(1.0 - u) / lambda;
    
    // Ensure we advance by at least 1 cycle to prevent infinite loops at the same timestamp
    uint64_t delta_cycles = std::max(uint64_t(1), static_cast<uint64_t>(std::round(delta)));
    
    return current_time + delta_cycles;
}
