// fault_trace.h — per-tick trace of the warp-control state of every core,
// used to see what a fault does to warp execution (PC jumps, warps dying,
// lanes switched off, mscratch corruption).
//
// Enabled only when FAULT_TRACE_FILE is set:
//   FAULT_TRACE_FILE=trace.txt      output file
//   FAULT_TRACE_START=<ts>          first timestamp to record (default 0)
//   FAULT_TRACE_STOP=<ts>           last timestamp to record (default: end)
//
// A line is written only when something changed, format:
//   <ts> c<k> act=<hex> stall=<hex> bar=<hex> tmask=<hex> pc0=<hex> pc1=<hex> msc=<hex>
// (one line per core that changed; pcN = byte address of warp N's PC).
//
// Written for NUM_CORES=2, NUM_WARPS=2, NUM_THREADS=8 (warp_pcs = 2 x 30 bits).
// Adjust FT_CORES / the PC decode if the configuration changes.

#ifndef FAULT_TRACE_H
#define FAULT_TRACE_H

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string>

#include "Vrtlsim_shim___024root.h"

#define FT_CORE_PREFIX rtlsim_shim__DOT__vortex__DOT__g_clusters__BRA__0__KET____DOT__cluster__DOT__g_sockets__BRA__0__KET____DOT__socket__DOT__g_cores__BRA__
// FT_SIG(0, schedule__DOT__warp_pcs) -> ..._g_cores__BRA__0__KET____DOT__core__DOT__schedule__DOT__warp_pcs
#define FT_SIG_(p, c, s) p##c##__KET____DOT__core__DOT__##s
#define FT_SIG_X(p, c, s) FT_SIG_(p, c, s)
#define FT_SIG(c, s) FT_SIG_X(FT_CORE_PREFIX, c, s)

struct FaultTraceCore {
    uint64_t warp_pcs;
    uint32_t mscratch;
    uint16_t thread_masks;
    uint8_t active, stalled, barrier;
    bool operator!=(const FaultTraceCore& o) const {
        return warp_pcs != o.warp_pcs || mscratch != o.mscratch || thread_masks != o.thread_masks ||
               active != o.active || stalled != o.stalled || barrier != o.barrier;
    }
};

class FaultTrace {
public:
    static constexpr int FT_CORES = 2;
    static constexpr int PC_BITS = 30;   // warp_pcs holds PC[31:2] per warp

    void configure() {
        const char* f = std::getenv("FAULT_TRACE_FILE");
        if (!f || !*f) return;
        out_ = std::fopen(f, "w");
        if (!out_) {
            std::fprintf(stderr, "[Fault Trace] cannot open %s\n", f);
            return;
        }
        if (const char* s = std::getenv("FAULT_TRACE_START")) start_ = std::strtoull(s, nullptr, 10);
        if (const char* s = std::getenv("FAULT_TRACE_STOP"))  stop_  = std::strtoull(s, nullptr, 10);
        std::fprintf(out_, "# ts core act stall bar tmask pc0 pc1 msc\n");
        std::fprintf(stderr, "[Fault Trace] writing %s (ts %llu..%llu)\n", f,
                     (unsigned long long)start_, (unsigned long long)stop_);
    }

    void sample(Vrtlsim_shim___024root* r, uint64_t ts) {
        if (!out_ || ts < start_ || ts > stop_) return;
        FaultTraceCore now[FT_CORES] = {
            read_core_0(r),
            read_core_1(r),
        };
        for (int c = 0; c < FT_CORES; ++c) {
            if (first_ || now[c] != last_[c]) {
                const FaultTraceCore& v = now[c];
                const uint64_t mask = (1ull << PC_BITS) - 1;
                unsigned long long pc0 = ((v.warp_pcs >> 0) & mask) << 2;
                unsigned long long pc1 = ((v.warp_pcs >> PC_BITS) & mask) << 2;
                std::fprintf(out_, "%llu c%d act=%x stall=%x bar=%x tmask=%04x pc0=%08llx pc1=%08llx msc=%08x\n",
                             (unsigned long long)ts, c, v.active, v.stalled, v.barrier, v.thread_masks,
                             pc0, pc1, v.mscratch);
                last_[c] = v;
            }
        }
        first_ = false;
    }

    void end(uint64_t ts) {
        if (out_) {
            std::fprintf(out_, "# end %llu\n", (unsigned long long)ts);
            std::fflush(out_);
        }
    }

    ~FaultTrace() {
        if (out_) std::fclose(out_);
    }

private:
#define FT_READ_CORE(c)                                                                         \
    static FaultTraceCore read_core_##c(Vrtlsim_shim___024root* r) {                            \
        FaultTraceCore v;                                                                       \
        v.warp_pcs     = r->FT_SIG(c, schedule__DOT__warp_pcs);                                 \
        v.thread_masks = r->FT_SIG(c, schedule__DOT__thread_masks);                             \
        v.active       = r->FT_SIG(c, schedule__DOT__active_warps);                             \
        v.stalled      = r->FT_SIG(c, schedule__DOT__stalled_warps);                            \
        v.barrier      = r->FT_SIG(c, schedule__DOT__barrier_masks);                            \
        v.mscratch     = r->FT_SIG(c, execute__DOT__sfu_unit__DOT__csr_unit__DOT__csr_data__DOT__mscratch); \
        return v;                                                                               \
    }
    FT_READ_CORE(0)
    FT_READ_CORE(1)
#undef FT_READ_CORE

    std::FILE* out_ = nullptr;
    uint64_t start_ = 0;
    uint64_t stop_ = UINT64_MAX;
    bool first_ = true;
    FaultTraceCore last_[FT_CORES] = {};
};

#endif // FAULT_TRACE_H
