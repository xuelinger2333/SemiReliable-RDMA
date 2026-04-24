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

namespace py = pybind11;
using namespace semirdma;

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
    py::class_<UCQPEngine>(m, "UCQPEngine")
        .def(py::init<const std::string&, size_t, int, int, int>(),
             py::arg("dev_name"),
             py::arg("buffer_bytes"),
             py::arg("sq_depth"),
             py::arg("rq_depth"),
             py::arg("gid_index") = -1)
        .def("bring_up", &UCQPEngine::bring_up, py::arg("remote"))
        .def("post_write", &UCQPEngine::post_write,
             py::arg("wr_id"),
             py::arg("local_offset"),
             py::arg("remote_offset"),
             py::arg("length"),
             py::arg("remote"),
             py::arg("with_imm"),
             py::arg("imm_data") = 0)
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
                ChunkSet& cs, double ratio, int timeout_ms)
             {
                 WaitStats stats;
                 bool ok;
                 {
                     py::gil_scoped_release release;
                     ok = self.wait_for_ratio(cs, ratio, timeout_ms, &stats);
                 }
                 py::dict s;
                 s["ok"]         = ok;
                 s["latency_ms"] = stats.latency_ms;
                 s["poll_count"] = stats.poll_count;
                 s["completed"]  = stats.completed;
                 s["timed_out"]  = stats.timed_out;
                 return s;
             },
             py::arg("cs"), py::arg("ratio"), py::arg("timeout_ms"));

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
}
