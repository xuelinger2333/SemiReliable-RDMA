/*
 * py_semirdma.cpp — pybind11 bridge for Phase 2 SemiRDMA transport
 *
 * Surface exposed to Python:
 *   UCQPEngine       — QP lifecycle + Write / Recv / poll
 *   ChunkSet         — Phase 2 chunking + completion bitmap
 *   RatioController  — CQE-driven ratio waiter (releases GIL while blocking)
 *   apply_ghost_mask — free function wrapping GhostMask::apply
 *   RemoteQpInfo / RemoteMR — small POD structs constructed by the TCP
 *                             bootstrap layer in Python
 *
 * Key decisions:
 *   - Zero-copy: local_buf_view returns py::memoryview over the registered
 *     MR buffer.  Python code does np.frombuffer() without copying.
 *   - GIL: blocking verbs calls (poll_cq with timeout > 0, wait_for_ratio)
 *     release the GIL so other Python threads can run.
 *   - No imports of Python types in C++ signatures beyond pybind11 primitives
 *     — keeps the binding unit-testable by loading the .so directly.
 */

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstring>
#include <string>
#include <vector>

#include "transport/uc_qp_engine.h"
#include "transport/chunk_manager.h"
#include "transport/ratio_controller.h"
#include "transport/ghost_mask.h"

#include "transport/clear/control_plane.h"
#include "transport/clear/control_plane_codec.h"
#include "transport/clear/finalizer.h"
#include "transport/clear/imm_codec.h"
#include "transport/clear/lease_table.h"
#include "transport/clear/messages.h"
#include "transport/clear/rq_monitor.h"
#include "transport/clear/witness_codec.h"

namespace py = pybind11;
using namespace semirdma;
namespace sc = semirdma::clear;

namespace {

// Convert an ibv_wc_opcode to a short string — easier to assert in tests
// than the raw enum value.
const char* opcode_name(ibv_wc_opcode op) {
    switch (op) {
        case IBV_WC_SEND:                 return "SEND";
        case IBV_WC_RDMA_WRITE:           return "RDMA_WRITE";
        case IBV_WC_RDMA_READ:            return "RDMA_READ";
        case IBV_WC_COMP_SWAP:            return "COMP_SWAP";
        case IBV_WC_FETCH_ADD:            return "FETCH_ADD";
        case IBV_WC_BIND_MW:              return "BIND_MW";
        case IBV_WC_RECV:                 return "RECV";
        case IBV_WC_RECV_RDMA_WITH_IMM:   return "RECV_RDMA_WITH_IMM";
        default:                          return "UNKNOWN";
    }
}

const char* status_name(ibv_wc_status s) {
    switch (s) {
        case IBV_WC_SUCCESS:               return "SUCCESS";
        case IBV_WC_LOC_LEN_ERR:           return "LOC_LEN_ERR";
        case IBV_WC_LOC_QP_OP_ERR:         return "LOC_QP_OP_ERR";
        case IBV_WC_LOC_EEC_OP_ERR:        return "LOC_EEC_OP_ERR";
        case IBV_WC_LOC_PROT_ERR:          return "LOC_PROT_ERR";
        case IBV_WC_WR_FLUSH_ERR:          return "WR_FLUSH_ERR";
        case IBV_WC_MW_BIND_ERR:           return "MW_BIND_ERR";
        case IBV_WC_REM_INV_REQ_ERR:       return "REM_INV_REQ_ERR";
        case IBV_WC_REM_ACCESS_ERR:        return "REM_ACCESS_ERR";
        case IBV_WC_REM_OP_ERR:            return "REM_OP_ERR";
        case IBV_WC_RETRY_EXC_ERR:         return "RETRY_EXC_ERR";
        case IBV_WC_RNR_RETRY_EXC_ERR:     return "RNR_RETRY_EXC_ERR";
        case IBV_WC_REM_ABORT_ERR:         return "REM_ABORT_ERR";
        case IBV_WC_FATAL_ERR:             return "FATAL_ERR";
        case IBV_WC_GENERAL_ERR:           return "GENERAL_ERR";
        default:                           return "UNKNOWN";
    }
}

// ibv_gid is a 16-byte union.  Expose as Python bytes (immutable, simple).
py::bytes gid_to_bytes(const ibv_gid& g) {
    return py::bytes(reinterpret_cast<const char*>(g.raw), 16);
}

ibv_gid gid_from_bytes(py::bytes b) {
    std::string s = b;
    if (s.size() != 16) {
        throw std::runtime_error("RemoteQpInfo.gid must be exactly 16 bytes");
    }
    ibv_gid g;
    std::memcpy(g.raw, s.data(), 16);
    return g;
}

} // namespace

PYBIND11_MODULE(_semirdma_ext, m) {
    m.doc() = "SemiRDMA Phase 2 transport — Python bindings (Phase 3 Stage A).";

    // ---- RemoteQpInfo --------------------------------------------------
    py::class_<RemoteQpInfo>(m, "RemoteQpInfo")
        .def(py::init([](uint32_t qpn, py::bytes gid) {
                 RemoteQpInfo info;
                 info.qpn = qpn;
                 info.gid = gid_from_bytes(gid);
                 return info;
             }),
             py::arg("qpn"), py::arg("gid"))
        .def_readwrite("qpn", &RemoteQpInfo::qpn)
        .def_property(
            "gid",
            [](const RemoteQpInfo& self) { return gid_to_bytes(self.gid); },
            [](RemoteQpInfo& self, py::bytes b) { self.gid = gid_from_bytes(b); })
        .def("__repr__", [](const RemoteQpInfo& self) {
            return "<RemoteQpInfo qpn=" + std::to_string(self.qpn) + ">";
        });

    // ---- RemoteMR ------------------------------------------------------
    py::class_<RemoteMR>(m, "RemoteMR")
        .def(py::init([](uint64_t addr, uint32_t rkey) {
                 return RemoteMR{addr, rkey};
             }),
             py::arg("addr"), py::arg("rkey"))
        .def_readwrite("addr", &RemoteMR::addr)
        .def_readwrite("rkey", &RemoteMR::rkey)
        .def("__repr__", [](const RemoteMR& self) {
            return "<RemoteMR addr=0x" + std::to_string(self.addr) +
                   " rkey=" + std::to_string(self.rkey) + ">";
        });

    // ---- UCQPEngine ----------------------------------------------------
    // Historically UC-only; ``qp_type="rc"`` enables HW-reliable RC mode
    // with the RC-specific RTR/RTS attrs.  RC params are no-ops for UC.
    py::class_<UCQPEngine>(m, "UCQPEngine")
        .def(py::init<const std::string&, size_t, int, int, int,
                      const std::string&, int, int, int, int, int>(),
             py::arg("dev_name"),
             py::arg("buffer_bytes"),
             py::arg("sq_depth"),
             py::arg("rq_depth"),
             py::arg("gid_index")        = -1,
             py::arg("qp_type")          = "uc",
             py::arg("rc_timeout")       = 14,
             py::arg("rc_retry_cnt")     = 7,
             py::arg("rc_rnr_retry")     = 7,
             py::arg("rc_min_rnr_timer") = 12,
             py::arg("rc_max_rd_atomic") = 1)
        .def("bring_up", &UCQPEngine::bring_up, py::arg("remote"))
        .def("post_write", &UCQPEngine::post_write,
             py::arg("wr_id"),
             py::arg("local_offset"),
             py::arg("remote_offset"),
             py::arg("length"),
             py::arg("remote"),
             py::arg("with_imm"),
             py::arg("imm_data") = 0)
        .def("post_bucket_chunks",
             [](UCQPEngine& self,
                size_t base_offset, size_t remote_base_offset,
                size_t total_bytes, size_t chunk_bytes,
                int sq_depth_throttle, int drain_timeout_ms,
                int per_wr_pace_us,
                const RemoteMR& remote, bool with_imm,
                uint64_t wr_id_base) {
                 py::gil_scoped_release release;
                 return self.post_bucket_chunks(
                     base_offset, remote_base_offset,
                     total_bytes, chunk_bytes,
                     sq_depth_throttle, drain_timeout_ms,
                     per_wr_pace_us,
                     remote, with_imm, wr_id_base);
             },
             py::arg("base_offset"),
             py::arg("remote_base_offset"),
             py::arg("total_bytes"),
             py::arg("chunk_bytes"),
             py::arg("sq_depth_throttle"),
             py::arg("drain_timeout_ms"),
             py::arg("per_wr_pace_us"),
             py::arg("remote"),
             py::arg("with_imm"),
             py::arg("wr_id_base"))
        .def("post_recv", &UCQPEngine::post_recv, py::arg("wr_id"))
        .def("post_recv_batch", &UCQPEngine::post_recv_batch,
             py::arg("n"), py::arg("base_wr_id") = 0)
        .def("outstanding_recv", &UCQPEngine::outstanding_recv)
        .def("poll_cq",
             [](UCQPEngine& self, int max_n, int timeout_ms) {
                 std::vector<Completion> cqes;
                 {
                     py::gil_scoped_release release;
                     cqes = self.poll_cq(max_n, timeout_ms);
                 }
                 py::list out;
                 for (const auto& c : cqes) {
                     py::dict d;
                     d["wr_id"]       = c.wr_id;
                     d["opcode"]      = static_cast<int>(c.opcode);
                     d["opcode_name"] = std::string(opcode_name(c.opcode));
                     d["status"]      = static_cast<int>(c.status);
                     d["status_name"] = std::string(status_name(c.status));
                     d["imm_data"]    = c.imm_data;
                     out.append(d);
                 }
                 return out;
             },
             py::arg("max_n"), py::arg("timeout_ms") = 0)
        .def("local_mr_info",  &UCQPEngine::local_mr_info)
        .def("local_qp_info",  &UCQPEngine::local_qp_info)
        .def_property_readonly("qpn",       &UCQPEngine::qpn)
        .def_property_readonly("buf_bytes", &UCQPEngine::buf_bytes)
        .def("local_buf_view",
             [](UCQPEngine& self) {
                 // Writable memoryview over the registered MR buffer.
                 // Python side typically wraps with np.frombuffer(..., dtype=...)
                 // for zero-copy tensor <-> MR byte-level access.
                 return py::memoryview::from_memory(
                     self.local_buf(),
                     static_cast<py::ssize_t>(self.buf_bytes()),
                     /*readonly=*/false);
             });

    // ---- ChunkSet ------------------------------------------------------
    py::class_<ChunkSet>(m, "ChunkSet")
        .def(py::init<size_t, size_t, size_t>(),
             py::arg("base_offset"),
             py::arg("total_bytes"),
             py::arg("chunk_bytes"))
        .def("size",             &ChunkSet::size)
        .def("num_completed",    &ChunkSet::num_completed)
        .def("completion_ratio", &ChunkSet::completion_ratio)
        .def("mark_completed",   &ChunkSet::mark_completed, py::arg("chunk_id"))
        .def("reset_states",     &ChunkSet::reset_states)
        .def_property_readonly("chunk_bytes", &ChunkSet::chunk_bytes)
        .def_property_readonly("total_bytes", &ChunkSet::total_bytes)
        .def_property_readonly("base_offset", &ChunkSet::base_offset)
        .def("chunk",
             [](const ChunkSet& self, size_t i) {
                 const auto& c = self.chunk(i);
                 py::dict d;
                 d["chunk_id"]      = c.chunk_id;
                 d["local_offset"]  = c.local_offset;
                 d["remote_offset"] = c.remote_offset;
                 d["length"]        = c.length;
                 return d;
             },
             py::arg("i"))
        .def("state",
             [](const ChunkSet& self, size_t i) {
                 const auto& s = self.state(i);
                 py::dict d;
                 d["has_cqe"]   = s.has_cqe;
                 d["valid_len"] = s.valid_len;
                 return d;
             },
             py::arg("i"));

    // ---- RatioController -----------------------------------------------
    py::class_<RatioController>(m, "RatioController")
        .def(py::init<UCQPEngine&>(), py::arg("engine"), py::keep_alive<1, 2>())
        .def("wait_for_ratio",
             [](RatioController& self,
                ChunkSet& cs, double ratio, int timeout_ms,
                uint8_t expected_bucket_id)
             {
                 WaitStats stats;
                 bool ok;
                 {
                     py::gil_scoped_release release;
                     ok = self.wait_for_ratio(cs, ratio, timeout_ms,
                                              expected_bucket_id, &stats);
                 }
                 py::dict s;
                 s["ok"]         = ok;
                 s["latency_ms"] = stats.latency_ms;
                 s["poll_count"] = stats.poll_count;
                 s["completed"]  = stats.completed;
                 s["timed_out"]  = stats.timed_out;
                 return s;
             },
             py::arg("cs"), py::arg("ratio"), py::arg("timeout_ms"),
             py::arg("expected_bucket_id") = static_cast<uint8_t>(0))
        .def("drain_pending", &RatioController::drain_pending,
             py::arg("cs"), py::arg("expected_bucket_id"))
        .def("stash_foreign", &RatioController::stash_foreign,
             py::arg("bucket_id"), py::arg("chunk_id"))
        .def("pending_size", &RatioController::pending_size)
        .def("pending_size_for", &RatioController::pending_size_for,
             py::arg("bucket_id"))
        .def("clear_pending", &RatioController::clear_pending)
        // ---- Phase 5 CLEAR-mode methods (additive) ----
        .def("wait_for_ratio_clear",
             [](RatioController& self,
                ChunkSet& cs, double ratio, int timeout_ms,
                uint8_t slot_id, uint8_t gen) {
                 RatioExitReason reason = RatioExitReason::DEADLINE;
                 WaitStats stats;
                 bool ok;
                 {
                     py::gil_scoped_release release;
                     ok = self.wait_for_ratio_clear(
                         cs, ratio, timeout_ms, slot_id, gen,
                         &reason, &stats);
                 }
                 py::dict d;
                 d["ok"]         = ok;
                 d["reason"]     = reason;
                 d["latency_ms"] = stats.latency_ms;
                 d["poll_count"] = stats.poll_count;
                 d["completed"]  = stats.completed;
                 d["timed_out"]  = stats.timed_out;
                 return d;
             },
             py::arg("cs"), py::arg("ratio"), py::arg("timeout_ms"),
             py::arg("slot_id"), py::arg("gen"))
        .def("clr_drain_pending", &RatioController::clr_drain_pending,
             py::arg("cs"), py::arg("slot_id"), py::arg("gen"))
        .def("clr_stash_foreign", &RatioController::clr_stash_foreign,
             py::arg("slot_id"), py::arg("gen"), py::arg("chunk_idx"))
        .def("clr_pending_size", &RatioController::clr_pending_size)
        .def("clr_pending_size_for", &RatioController::clr_pending_size_for,
             py::arg("slot_id"), py::arg("gen"))
        .def("clr_clear_pending", &RatioController::clr_clear_pending);

    // ---- Free function: apply_ghost_mask --------------------------------
    m.def("apply_ghost_mask",
          [](py::buffer buf, const ChunkSet& cs) {
              py::buffer_info info = buf.request(/*writable=*/true);
              if (info.itemsize != 1) {
                  throw std::runtime_error(
                      "apply_ghost_mask expects a byte-level buffer (itemsize=1)");
              }
              if (info.ndim != 1) {
                  throw std::runtime_error(
                      "apply_ghost_mask expects a 1-D buffer");
              }
              if (static_cast<size_t>(info.size) <
                      cs.base_offset() + cs.total_bytes()) {
                  throw std::runtime_error(
                      "apply_ghost_mask: buffer shorter than base + total_bytes");
              }
              GhostMask::apply(
                  reinterpret_cast<uint8_t*>(info.ptr),
                  cs);
          },
          py::arg("buf"), py::arg("cs"),
          "Zero-out buffer regions for chunks without CQE. "
          "`buf` must be a writable 1-byte-itemsize buffer covering "
          "[0, cs.base_offset + cs.total_bytes).");

    // -----------------------------------------------------------------
    // Phase 5 CLEAR — RatioExitReason enum (used by RatioController.
    // wait_for_ratio_clear, registered in the RatioController binding
    // above).
    // -----------------------------------------------------------------
    py::enum_<RatioExitReason>(m, "RatioExitReason")
        .value("DELIVERED", RatioExitReason::DELIVERED)
        .value("RATIO_MET", RatioExitReason::RATIO_MET)
        .value("DEADLINE",  RatioExitReason::DEADLINE);

    // -----------------------------------------------------------------
    // Phase 5 CLEAR — submodule for the new types
    // -----------------------------------------------------------------
    py::module_ clear_mod = m.def_submodule(
        "clear",
        "CLEAR (Completion-Labeled Erasure Attribution for RoCE UC) types.");

    // ---- enums (must mirror clear::Policy / FinalizeDecision /
    //      WitnessEncoding values) ------------------------------------
    py::enum_<sc::Policy>(clear_mod, "Policy")
        .value("REPAIR_FIRST",    sc::Policy::REPAIR_FIRST)
        .value("MASK_FIRST",      sc::Policy::MASK_FIRST)
        .value("STALE_FILL",      sc::Policy::STALE_FILL)
        .value("ESTIMATOR_SCALE", sc::Policy::ESTIMATOR_SCALE);

    py::enum_<sc::FinalizeDecision>(clear_mod, "FinalizeDecision")
        .value("DELIVERED",   sc::FinalizeDecision::DELIVERED)
        .value("REPAIRED",    sc::FinalizeDecision::REPAIRED)
        .value("MASKED",      sc::FinalizeDecision::MASKED)
        .value("STALE",       sc::FinalizeDecision::STALE)
        .value("FALLBACK_RC", sc::FinalizeDecision::FALLBACK_RC);

    py::enum_<sc::WitnessEncoding>(clear_mod, "WitnessEncoding")
        .value("FULL_ALL_PRESENT", sc::WitnessEncoding::FULL_ALL_PRESENT)
        .value("FULL_ALL_ABSENT",  sc::WitnessEncoding::FULL_ALL_ABSENT)
        .value("RAW",              sc::WitnessEncoding::RAW)
        .value("RANGE_MISSING",    sc::WitnessEncoding::RANGE_MISSING);

    py::enum_<sc::LookupOutcome>(clear_mod, "LookupOutcome")
        .value("HIT",       sc::LookupOutcome::HIT)
        .value("PRE_BEGIN", sc::LookupOutcome::PRE_BEGIN)
        .value("STALE",     sc::LookupOutcome::STALE);

    // ---- Range / SlotPressure / LookupResult / PendingEntry ---------
    py::class_<sc::Range>(clear_mod, "Range")
        .def(py::init([](uint32_t start, uint32_t length) {
                 return sc::Range{start, length};
             }),
             py::arg("start"), py::arg("length"))
        .def_readwrite("start",  &sc::Range::start)
        .def_readwrite("length", &sc::Range::length)
        .def("__repr__", [](const sc::Range& r) {
            return "<Range start=" + std::to_string(r.start) +
                   " length=" + std::to_string(r.length) + ">";
        });

    py::class_<sc::SlotPressure>(clear_mod, "SlotPressure")
        .def_readonly("in_use",    &sc::SlotPressure::in_use)
        .def_readonly("near_wrap", &sc::SlotPressure::near_wrap)
        .def_readonly("total",     &sc::SlotPressure::total);

    py::class_<sc::LookupResult>(clear_mod, "LookupResult")
        .def_readonly("outcome", &sc::LookupResult::outcome)
        .def_readonly("uid",     &sc::LookupResult::uid);

    py::class_<sc::PendingEntry>(clear_mod, "PendingEntry")
        .def_readonly("slot_id",       &sc::PendingEntry::slot_id)
        .def_readonly("gen",           &sc::PendingEntry::gen)
        .def_readonly("chunk_idx",     &sc::PendingEntry::chunk_idx)
        .def_readonly("enqueued_tick", &sc::PendingEntry::enqueued_tick);

    // ---- imm_codec helpers ------------------------------------------
    clear_mod.def("encode_imm", &sc::encode_imm,
                  py::arg("slot_id"), py::arg("chunk_idx"), py::arg("gen"));
    clear_mod.def("imm_slot",  &sc::imm_slot,  py::arg("imm"));
    clear_mod.def("imm_chunk", &sc::imm_chunk, py::arg("imm"));
    clear_mod.def("imm_gen",   &sc::imm_gen,   py::arg("imm"));
    clear_mod.def("lease_key", &sc::lease_key,
                  py::arg("slot_id"), py::arg("gen"));
    clear_mod.attr("kImmGenMask")     = sc::kImmGenMask;
    clear_mod.attr("kImmChunkMask")   = sc::kImmChunkMask;
    clear_mod.attr("kImmSlotShift")   = sc::kImmSlotShift;
    clear_mod.attr("kImmChunkShift")  = sc::kImmChunkShift;
    clear_mod.attr("kImmMaxChunkIdx") = sc::kImmMaxChunkIdx;

    // ---- SenderLeaseTable -------------------------------------------
    py::class_<sc::SenderLeaseTable>(clear_mod, "SenderLeaseTable")
        .def(py::init<uint64_t>(),
             py::arg("quarantine_ticks") = sc::kDefaultQuarantineTicks)
        .def("acquire",
             [](sc::SenderLeaseTable& self, uint64_t uid,
                py::object slot_pref) {
                 std::optional<uint8_t> hint;
                 if (!slot_pref.is_none()) {
                     hint = slot_pref.cast<uint8_t>();
                 }
                 auto r = self.acquire(uid, hint);
                 py::dict d;
                 d["ok"]      = r.ok;
                 d["slot_id"] = r.slot_id;
                 d["gen"]     = r.gen;
                 return d;
             },
             py::arg("uid"), py::arg("slot_pref") = py::none())
        .def("release",  &sc::SenderLeaseTable::release, py::arg("uid"))
        .def("tick",     &sc::SenderLeaseTable::tick, py::arg("delta") = 1)
        .def("now",      &sc::SenderLeaseTable::now)
        .def("pressure", &sc::SenderLeaseTable::pressure)
        .def("peek",
             [](const sc::SenderLeaseTable& self, uint64_t uid) -> py::object {
                 auto r = self.peek(uid);
                 if (!r.has_value()) return py::none();
                 return py::make_tuple(r->first, r->second);
             },
             py::arg("uid"));

    // ---- ReceiverLeaseTable -----------------------------------------
    py::class_<sc::ReceiverLeaseTable>(clear_mod, "ReceiverLeaseTable")
        .def(py::init<size_t>(), py::arg("pending_capacity") = 4096)
        .def("install", &sc::ReceiverLeaseTable::install,
             py::arg("uid"), py::arg("slot_id"), py::arg("gen"))
        .def("lookup",  &sc::ReceiverLeaseTable::lookup,
             py::arg("slot_id"), py::arg("gen"))
        .def("retire",  &sc::ReceiverLeaseTable::retire, py::arg("uid"))
        .def("enqueue_pending", &sc::ReceiverLeaseTable::enqueue_pending,
             py::arg("slot_id"), py::arg("gen"), py::arg("chunk_idx"))
        .def("drain_pending_for",
             &sc::ReceiverLeaseTable::drain_pending_for,
             py::arg("slot_id"), py::arg("gen"))
        .def("expire_pending", &sc::ReceiverLeaseTable::expire_pending,
             py::arg("max_age_ticks"))
        .def("tick", &sc::ReceiverLeaseTable::tick, py::arg("delta") = 1)
        .def("now",  &sc::ReceiverLeaseTable::now)
        .def("pending_size",    &sc::ReceiverLeaseTable::pending_size)
        .def("pending_dropped", &sc::ReceiverLeaseTable::pending_dropped)
        .def("pressure",        &sc::ReceiverLeaseTable::pressure);

    // ---- decide_finalize free function (pure decision kernel) -------
    clear_mod.def(
        "decide_finalize",
        [](uint32_t n_chunks, py::buffer recv_bitmap, uint32_t chunk_bytes,
           sc::Policy policy, uint64_t repair_budget_bytes,
           uint64_t max_repair_bytes_per_uid) {
            py::buffer_info info = recv_bitmap.request(/*writable=*/false);
            if (info.itemsize != 1 || info.ndim != 1) {
                throw std::runtime_error(
                    "decide_finalize: recv_bitmap must be 1-D byte buffer");
            }
            auto r = sc::decide_finalize(
                n_chunks,
                reinterpret_cast<const uint8_t*>(info.ptr),
                static_cast<size_t>(info.size),
                chunk_bytes, policy,
                repair_budget_bytes, max_repair_bytes_per_uid);
            py::list ranges;
            for (const auto& rg : r.repair_ranges) {
                ranges.append(py::make_tuple(rg.start, rg.length));
            }
            py::dict d;
            d["decision"]              = r.decision;
            d["repair_ranges"]         = ranges;
            d["missing_count"]         = r.missing_count;
            d["missing_bytes"]         = r.missing_bytes;
            d["budget_consumed_bytes"] = r.budget_consumed_bytes;
            return d;
        },
        py::arg("n_chunks"), py::arg("recv_bitmap"), py::arg("chunk_bytes"),
        py::arg("policy"), py::arg("repair_budget_bytes"),
        py::arg("max_repair_bytes_per_uid") = 0);

    // ---- FinalizerConfig / FinalizerStats ---------------------------
    py::class_<sc::FinalizerConfig>(clear_mod, "FinalizerConfig")
        .def(py::init([](uint64_t budget, uint64_t cap) {
                 sc::FinalizerConfig c;
                 c.repair_budget_bytes_per_step = budget;
                 c.max_repair_bytes_per_uid     = cap;
                 return c;
             }),
             py::arg("repair_budget_bytes_per_step") = 16ull * 1024 * 1024,
             py::arg("max_repair_bytes_per_uid")     = 0)
        .def_readwrite("repair_budget_bytes_per_step",
                       &sc::FinalizerConfig::repair_budget_bytes_per_step)
        .def_readwrite("max_repair_bytes_per_uid",
                       &sc::FinalizerConfig::max_repair_bytes_per_uid);

    py::class_<sc::FinalizerStats>(clear_mod, "FinalizerStats")
        .def_readonly("n_uids_seen",        &sc::FinalizerStats::n_uids_seen)
        .def_readonly("n_finalized",        &sc::FinalizerStats::n_finalized)
        .def_readonly("total_repair_bytes",
                      &sc::FinalizerStats::total_repair_bytes)
        .def_readonly("budget_refills",     &sc::FinalizerStats::budget_refills)
        .def_readonly("budget_underruns",   &sc::FinalizerStats::budget_underruns)
        .def_property_readonly("n_decisions", [](const sc::FinalizerStats& s) {
            py::dict d;
            d["DELIVERED"]   = s.n_decisions[(int)sc::FinalizeDecision::DELIVERED];
            d["REPAIRED"]    = s.n_decisions[(int)sc::FinalizeDecision::REPAIRED];
            d["MASKED"]      = s.n_decisions[(int)sc::FinalizeDecision::MASKED];
            d["STALE"]       = s.n_decisions[(int)sc::FinalizeDecision::STALE];
            d["FALLBACK_RC"] = s.n_decisions[(int)sc::FinalizeDecision::FALLBACK_RC];
            return d;
        });

    // ---- Finalizer --------------------------------------------------
    // Callbacks marshalled as Python callables. The mask_bitmap pointers
    // passed to send_finalize / apply_mask are non-owning into a
    // std::vector that lives only for the duration of the C++ call;
    // we copy into py::bytes before invoking the Python callback.
    py::class_<sc::Finalizer>(clear_mod, "Finalizer")
        .def(py::init<sc::FinalizerConfig>(),
             py::arg("cfg") = sc::FinalizerConfig{})
        .def("on_send_repair_req",
             [](sc::Finalizer& self, py::function cb) {
                 self.on_send_repair_req(
                     [cb = std::move(cb)](uint64_t uid,
                                          const sc::Range* ranges,
                                          uint16_t n) {
                         py::gil_scoped_acquire gil;
                         py::list rs;
                         for (uint16_t i = 0; i < n; ++i) {
                             rs.append(py::make_tuple(ranges[i].start,
                                                       ranges[i].length));
                         }
                         cb(uid, rs);
                     });
             })
        .def("on_send_finalize",
             [](sc::Finalizer& self, py::function cb) {
                 self.on_send_finalize(
                     [cb = std::move(cb)](uint64_t uid,
                                          sc::FinalizeDecision d,
                                          sc::WitnessEncoding enc,
                                          const uint8_t* body, size_t len) {
                         py::gil_scoped_acquire gil;
                         py::bytes payload(
                             body ? reinterpret_cast<const char*>(body) : "",
                             len);
                         cb(uid, d, enc, payload);
                     });
             })
        .def("on_send_retire",
             [](sc::Finalizer& self, py::function cb) {
                 self.on_send_retire(
                     [cb = std::move(cb)](uint64_t uid, uint8_t slot,
                                          uint8_t gen) {
                         py::gil_scoped_acquire gil;
                         cb(uid, slot, gen);
                     });
             })
        .def("on_apply_mask",
             [](sc::Finalizer& self, py::function cb) {
                 self.on_apply_mask(
                     [cb = std::move(cb)](uint64_t uid,
                                          sc::FinalizeDecision d,
                                          const uint8_t* mask, size_t len,
                                          uint32_t n_chunks) {
                         py::gil_scoped_acquire gil;
                         py::bytes payload(
                             mask ? reinterpret_cast<const char*>(mask) : "",
                             len);
                         cb(uid, d, payload, n_chunks);
                     });
             })
        .def("track", &sc::Finalizer::track,
             py::arg("uid"), py::arg("slot"), py::arg("gen"),
             py::arg("n_chunks"), py::arg("chunk_bytes"), py::arg("policy"))
        .def("on_witness",
             [](sc::Finalizer& self, uint64_t uid, py::buffer recv_bitmap) {
                 py::buffer_info info = recv_bitmap.request(/*writable=*/false);
                 if (info.itemsize != 1 || info.ndim != 1) {
                     throw std::runtime_error(
                         "on_witness: recv_bitmap must be 1-D byte buffer");
                 }
                 return self.on_witness(
                     uid,
                     reinterpret_cast<const uint8_t*>(info.ptr),
                     static_cast<size_t>(info.size));
             },
             py::arg("uid"), py::arg("recv_bitmap"))
        .def("on_repair_complete", &sc::Finalizer::on_repair_complete,
             py::arg("uid"))
        .def("on_step_boundary",   &sc::Finalizer::on_step_boundary)
        .def("is_tracked",         &sc::Finalizer::is_tracked, py::arg("uid"))
        .def("repair_budget_remaining_bytes",
             &sc::Finalizer::repair_budget_remaining_bytes)
        .def_property_readonly("stats", &sc::Finalizer::stats);

    // ---- RQMonitor ---------------------------------------------------
    py::class_<sc::RQMonitorConfig>(clear_mod, "RQMonitorConfig")
        .def(py::init([](int32_t low, int32_t target, int32_t init) {
                 sc::RQMonitorConfig c;
                 c.low_watermark   = low;
                 c.refill_target   = target;
                 c.initial_credits = init;
                 return c;
             }),
             py::arg("low_watermark")   = 16,
             py::arg("refill_target")   = 64,
             py::arg("initial_credits") = 64)
        .def_readwrite("low_watermark",   &sc::RQMonitorConfig::low_watermark)
        .def_readwrite("refill_target",   &sc::RQMonitorConfig::refill_target)
        .def_readwrite("initial_credits", &sc::RQMonitorConfig::initial_credits);

    py::class_<sc::RQMonitorStats>(clear_mod, "RQMonitorStats")
        .def_readonly("low_watermark_events",
                      &sc::RQMonitorStats::low_watermark_events)
        .def_readonly("replenish_events", &sc::RQMonitorStats::replenish_events)
        .def_readonly("total_consumed",   &sc::RQMonitorStats::total_consumed)
        .def_readonly("total_posted",     &sc::RQMonitorStats::total_posted);

    py::class_<sc::RQMonitor>(clear_mod, "RQMonitor")
        .def(py::init<sc::RQMonitorConfig>(),
             py::arg("cfg") = sc::RQMonitorConfig{})
        .def("on_low_watermark",
             [](sc::RQMonitor& self, py::function cb) {
                 self.on_low_watermark(
                     [cb = std::move(cb)](uint16_t peer, int32_t credits) {
                         py::gil_scoped_acquire gil;
                         cb(peer, credits);
                     });
             })
        .def("on_replenish_request",
             [](sc::RQMonitor& self, py::function cb) {
                 self.on_replenish_request(
                     [cb = std::move(cb)](uint16_t peer, int32_t k) {
                         py::gil_scoped_acquire gil;
                         cb(peer, k);
                     });
             })
        .def("register_peer",   &sc::RQMonitor::register_peer,
             py::arg("peer_edge"))
        .def("record_consumed", &sc::RQMonitor::record_consumed,
             py::arg("peer_edge"), py::arg("n") = 1)
        .def("record_posted",   &sc::RQMonitor::record_posted,
             py::arg("peer_edge"), py::arg("n"))
        .def("credits", &sc::RQMonitor::credits, py::arg("peer_edge"))
        .def("is_low",  &sc::RQMonitor::is_low,  py::arg("peer_edge"))
        .def_property_readonly("stats", &sc::RQMonitor::stats);

    // ---- witness_codec encode/decode (for Python shadow oracle) -----
    clear_mod.def(
        "encode_witness",
        [](py::buffer bitmap, uint32_t n_chunks) {
            py::buffer_info info = bitmap.request(/*writable=*/false);
            if (info.itemsize != 1 || info.ndim != 1) {
                throw std::runtime_error(
                    "encode_witness: bitmap must be 1-D byte buffer");
            }
            auto r = sc::encode_witness(
                reinterpret_cast<const uint8_t*>(info.ptr),
                static_cast<size_t>(info.size), n_chunks);
            py::dict d;
            d["encoding"]   = r.encoding;
            d["body"]       = py::bytes(
                reinterpret_cast<const char*>(r.body.data()), r.body.size());
            d["recv_count"] = r.recv_count;
            return d;
        },
        py::arg("bitmap"), py::arg("n_chunks"));

    clear_mod.def(
        "decode_witness",
        [](sc::WitnessEncoding encoding, py::buffer body, uint32_t n_chunks) {
            py::buffer_info info = body.request(/*writable=*/false);
            if (info.itemsize != 1 || info.ndim != 1) {
                throw std::runtime_error(
                    "decode_witness: body must be 1-D byte buffer");
            }
            std::vector<uint8_t> out;
            uint32_t recv_count = 0;
            bool ok = sc::decode_witness(
                encoding,
                reinterpret_cast<const uint8_t*>(info.ptr),
                static_cast<size_t>(info.size),
                n_chunks, out, recv_count);
            py::dict d;
            d["ok"]         = ok;
            d["bitmap"]     = py::bytes(
                reinterpret_cast<const char*>(out.data()), out.size());
            d["recv_count"] = recv_count;
            return d;
        },
        py::arg("encoding"), py::arg("body"), py::arg("n_chunks"));

    // -----------------------------------------------------------------
    // BeginPayload / RetirePayload / BackpressurePayload POD wrappers
    // -----------------------------------------------------------------
    py::class_<sc::BeginPayload>(clear_mod, "BeginPayload")
        .def(py::init([](uint8_t slot_id, uint8_t gen, uint8_t phase_id,
                         sc::Policy policy, uint16_t peer_edge,
                         uint32_t step_seq, uint32_t bucket_seq,
                         uint32_t n_chunks, uint32_t deadline_us,
                         uint32_t chunk_bytes, uint32_t checksum_seed) {
                 sc::BeginPayload p{};
                 p.slot_id       = slot_id;
                 p.gen           = gen;
                 p.phase_id      = phase_id;
                 p.policy        = static_cast<uint8_t>(policy);
                 p.peer_edge     = peer_edge;
                 p.step_seq      = step_seq;
                 p.bucket_seq    = bucket_seq;
                 p.n_chunks      = n_chunks;
                 p.deadline_us   = deadline_us;
                 p.chunk_bytes   = chunk_bytes;
                 p.checksum_seed = checksum_seed;
                 return p;
             }),
             py::arg("slot_id"), py::arg("gen"), py::arg("phase_id") = 0,
             py::arg("policy") = sc::Policy::MASK_FIRST,
             py::arg("peer_edge") = 0,
             py::arg("step_seq") = 0, py::arg("bucket_seq") = 0,
             py::arg("n_chunks") = 0, py::arg("deadline_us") = 200000,
             py::arg("chunk_bytes") = 4096,
             py::arg("checksum_seed") = 0)
        .def_readwrite("slot_id",       &sc::BeginPayload::slot_id)
        .def_readwrite("gen",           &sc::BeginPayload::gen)
        .def_readwrite("phase_id",      &sc::BeginPayload::phase_id)
        .def_readwrite("policy",        &sc::BeginPayload::policy)
        .def_readwrite("peer_edge",     &sc::BeginPayload::peer_edge)
        .def_readwrite("step_seq",      &sc::BeginPayload::step_seq)
        .def_readwrite("bucket_seq",    &sc::BeginPayload::bucket_seq)
        .def_readwrite("n_chunks",      &sc::BeginPayload::n_chunks)
        .def_readwrite("deadline_us",   &sc::BeginPayload::deadline_us)
        .def_readwrite("chunk_bytes",   &sc::BeginPayload::chunk_bytes)
        .def_readwrite("checksum_seed", &sc::BeginPayload::checksum_seed);

    py::class_<sc::RetirePayload>(clear_mod, "RetirePayload")
        .def(py::init([](uint8_t slot_id, uint8_t gen) {
                 sc::RetirePayload p{};
                 p.slot_id = slot_id;
                 p.gen     = gen;
                 return p;
             }),
             py::arg("slot_id"), py::arg("gen"))
        .def_readwrite("slot_id", &sc::RetirePayload::slot_id)
        .def_readwrite("gen",     &sc::RetirePayload::gen);

    py::class_<sc::BackpressurePayload>(clear_mod, "BackpressurePayload")
        .def(py::init([](uint16_t peer_edge, uint16_t requested_credits) {
                 sc::BackpressurePayload p{};
                 p.peer_edge         = peer_edge;
                 p.requested_credits = requested_credits;
                 return p;
             }),
             py::arg("peer_edge"), py::arg("requested_credits"))
        .def_readwrite("peer_edge",         &sc::BackpressurePayload::peer_edge)
        .def_readwrite("requested_credits",
                       &sc::BackpressurePayload::requested_credits);

    // -----------------------------------------------------------------
    // ControlPlaneConfig / ControlPlaneStats
    // -----------------------------------------------------------------
    py::class_<sc::ControlPlaneConfig>(clear_mod, "ControlPlaneConfig")
        .def(py::init([](const std::string& dev, int gid_idx,
                         uint16_t recv_slots, uint16_t send_slots) {
                 sc::ControlPlaneConfig c;
                 c.dev_name   = dev;
                 c.gid_index  = gid_idx;
                 c.recv_slots = recv_slots;
                 c.send_slots = send_slots;
                 return c;
             }),
             py::arg("dev_name"),
             py::arg("gid_index")  = -1,
             py::arg("recv_slots") = 64,
             py::arg("send_slots") = 16)
        .def_readwrite("dev_name",   &sc::ControlPlaneConfig::dev_name)
        .def_readwrite("gid_index",  &sc::ControlPlaneConfig::gid_index)
        .def_readwrite("recv_slots", &sc::ControlPlaneConfig::recv_slots)
        .def_readwrite("send_slots", &sc::ControlPlaneConfig::send_slots);

    py::class_<sc::ControlPlaneStats>(clear_mod, "ControlPlaneStats")
        .def_readonly("sent_total",            &sc::ControlPlaneStats::sent_total)
        .def_readonly("recv_total",            &sc::ControlPlaneStats::recv_total)
        .def_readonly("recv_decode_errors",
                      &sc::ControlPlaneStats::recv_decode_errors)
        .def_readonly("send_completion_errors",
                      &sc::ControlPlaneStats::send_completion_errors)
        .def_readonly("recv_dropped_full",
                      &sc::ControlPlaneStats::recv_dropped_full)
        .def_property_readonly("sent_by_type",
            [](const sc::ControlPlaneStats& s) {
                py::dict d;
                d["BEGIN"]        = s.sent_by_type[(int)sc::MsgType::BEGIN];
                d["WITNESS"]      = s.sent_by_type[(int)sc::MsgType::WITNESS];
                d["REPAIR_REQ"]   = s.sent_by_type[(int)sc::MsgType::REPAIR_REQ];
                d["FINALIZE"]     = s.sent_by_type[(int)sc::MsgType::FINALIZE];
                d["RETIRE"]       = s.sent_by_type[(int)sc::MsgType::RETIRE];
                d["BACKPRESSURE"] = s.sent_by_type[(int)sc::MsgType::BACKPRESSURE];
                return d;
            })
        .def_property_readonly("recv_by_type",
            [](const sc::ControlPlaneStats& s) {
                py::dict d;
                d["BEGIN"]        = s.recv_by_type[(int)sc::MsgType::BEGIN];
                d["WITNESS"]      = s.recv_by_type[(int)sc::MsgType::WITNESS];
                d["REPAIR_REQ"]   = s.recv_by_type[(int)sc::MsgType::REPAIR_REQ];
                d["FINALIZE"]     = s.recv_by_type[(int)sc::MsgType::FINALIZE];
                d["RETIRE"]       = s.recv_by_type[(int)sc::MsgType::RETIRE];
                d["BACKPRESSURE"] = s.recv_by_type[(int)sc::MsgType::BACKPRESSURE];
                return d;
            });

    // -----------------------------------------------------------------
    // ControlPlane — RC QP wrapper. Variable-length bodies are copied
    // into py::bytes before invoking Python callbacks (the C++
    // ParsedXxx structs hold non-owning views into recv buffers that
    // get reposted right after dispatch).
    // -----------------------------------------------------------------
    py::class_<sc::ControlPlane>(clear_mod, "ControlPlane")
        .def(py::init<sc::ControlPlaneConfig>(), py::arg("cfg"))
        .def("bring_up", &sc::ControlPlane::bring_up, py::arg("peer"))
        .def("local_qp_info", &sc::ControlPlane::local_qp_info)
        .def("local_mr_info", &sc::ControlPlane::local_mr_info)
        .def("send_begin",
             [](sc::ControlPlane& self, uint64_t uid,
                const sc::BeginPayload& p) {
                 return self.send_begin(uid, p);
             },
             py::arg("uid"), py::arg("payload"))
        .def("send_witness",
             [](sc::ControlPlane& self, uint64_t uid, uint32_t recv_count,
                sc::WitnessEncoding encoding, py::buffer body) {
                 py::buffer_info info = body.request(/*writable=*/false);
                 if (info.itemsize != 1 || info.ndim != 1) {
                     throw std::runtime_error(
                         "send_witness: body must be 1-D byte buffer");
                 }
                 return self.send_witness(
                     uid, recv_count, encoding,
                     reinterpret_cast<const uint8_t*>(info.ptr),
                     static_cast<size_t>(info.size));
             },
             py::arg("uid"), py::arg("recv_count"), py::arg("encoding"),
             py::arg("body"))
        .def("send_repair_req",
             [](sc::ControlPlane& self, uint64_t uid, py::list ranges) {
                 std::vector<sc::Range> rs;
                 rs.reserve(ranges.size());
                 for (auto h : ranges) {
                     auto t = h.cast<py::tuple>();
                     if (t.size() != 2) {
                         throw std::runtime_error(
                             "send_repair_req: each range must be (start, length)");
                     }
                     rs.push_back(sc::Range{t[0].cast<uint32_t>(),
                                            t[1].cast<uint32_t>()});
                 }
                 return self.send_repair_req(
                     uid, rs.data(), static_cast<uint16_t>(rs.size()));
             },
             py::arg("uid"), py::arg("ranges"))
        .def("send_finalize",
             [](sc::ControlPlane& self, uint64_t uid,
                sc::FinalizeDecision decision,
                sc::WitnessEncoding mask_encoding, py::buffer mask_body) {
                 py::buffer_info info = mask_body.request(/*writable=*/false);
                 if (info.itemsize != 1 || info.ndim != 1) {
                     throw std::runtime_error(
                         "send_finalize: mask_body must be 1-D byte buffer");
                 }
                 return self.send_finalize(
                     uid, decision, mask_encoding,
                     reinterpret_cast<const uint8_t*>(info.ptr),
                     static_cast<size_t>(info.size));
             },
             py::arg("uid"), py::arg("decision"),
             py::arg("mask_encoding"), py::arg("mask_body"))
        .def("send_retire",
             [](sc::ControlPlane& self, uint64_t uid,
                const sc::RetirePayload& p) {
                 return self.send_retire(uid, p);
             },
             py::arg("uid"), py::arg("payload"))
        .def("send_backpressure",
             [](sc::ControlPlane& self, uint64_t uid,
                const sc::BackpressurePayload& p) {
                 return self.send_backpressure(uid, p);
             },
             py::arg("uid"), py::arg("payload"))
        .def("poll_once",
             [](sc::ControlPlane& self, int max_completions, int timeout_ms) {
                 py::gil_scoped_release release;
                 return self.poll_once(max_completions, timeout_ms);
             },
             py::arg("max_completions") = 32, py::arg("timeout_ms") = 0)
        .def("on_begin",
             [](sc::ControlPlane& self, py::function cb) {
                 self.on_begin(
                     [cb = std::move(cb)](const sc::ParsedBegin& b) {
                         py::gil_scoped_acquire gil;
                         cb(b.uid,
                            b.payload.slot_id, b.payload.gen,
                            b.payload.phase_id, b.payload.policy,
                            b.payload.peer_edge, b.payload.step_seq,
                            b.payload.bucket_seq, b.payload.n_chunks,
                            b.payload.deadline_us, b.payload.chunk_bytes,
                            b.payload.checksum_seed);
                     });
             })
        .def("on_witness",
             [](sc::ControlPlane& self, py::function cb) {
                 self.on_witness(
                     [cb = std::move(cb)](const sc::ParsedWitness& w) {
                         py::gil_scoped_acquire gil;
                         py::bytes body(
                             w.body ? reinterpret_cast<const char*>(w.body) : "",
                             w.body_len);
                         cb(w.uid, w.recv_count, w.encoding, body);
                     });
             })
        .def("on_repair_req",
             [](sc::ControlPlane& self, py::function cb) {
                 self.on_repair_req(
                     [cb = std::move(cb)](const sc::ParsedRepairReq& r) {
                         py::gil_scoped_acquire gil;
                         py::list rs;
                         for (uint16_t i = 0; i < r.n_ranges; ++i) {
                             rs.append(py::make_tuple(
                                 r.ranges[i].start, r.ranges[i].length));
                         }
                         cb(r.uid, rs);
                     });
             })
        .def("on_finalize",
             [](sc::ControlPlane& self, py::function cb) {
                 self.on_finalize(
                     [cb = std::move(cb)](const sc::ParsedFinalize& f) {
                         py::gil_scoped_acquire gil;
                         py::bytes body(
                             f.mask_body
                                 ? reinterpret_cast<const char*>(f.mask_body)
                                 : "",
                             f.mask_body_len);
                         cb(f.uid, f.decision, f.mask_encoding, body);
                     });
             })
        .def("on_retire",
             [](sc::ControlPlane& self, py::function cb) {
                 self.on_retire(
                     [cb = std::move(cb)](const sc::ParsedRetire& r) {
                         py::gil_scoped_acquire gil;
                         cb(r.uid, r.payload.slot_id, r.payload.gen);
                     });
             })
        .def("on_backpressure",
             [](sc::ControlPlane& self, py::function cb) {
                 self.on_backpressure(
                     [cb = std::move(cb)](const sc::ParsedBackpressure& b) {
                         py::gil_scoped_acquire gil;
                         cb(b.uid, b.payload.peer_edge,
                            b.payload.requested_credits);
                     });
             })
        .def_property_readonly("stats", &sc::ControlPlane::stats);
}
