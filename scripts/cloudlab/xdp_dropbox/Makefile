# XDP loss-injection forwarder — BPF object build
#
# Produces:
#   xdp_dropbox.bpf.o      the loadable BPF object
#
# Requirements on the middlebox node (installed by middlebox_setup.sh
# bootstrap):
#   clang >= 11   (Ubuntu 22.04: apt install clang llvm)
#   libbpf-dev    (headers for bpf_helpers.h, bpf_endian.h)
#   bpftool       (for map/prog load, attach, and stats read)
#
# The compiled .o is loaded by middlebox_setup.sh via bpftool.

CLANG    ?= clang
BPFTOOL  ?= bpftool
ARCH     ?= $(shell uname -m | sed 's/x86_64/x86/; s/aarch64/arm64/')
KERNEL_RELEASE ?= $(shell uname -r)

# libbpf headers are in /usr/include/bpf/ on Ubuntu 22.04's libbpf-dev.
# Kernel headers (for linux/bpf.h etc.) are in /usr/include/ or
# /usr/src/linux-headers-*/include/.
BPF_CFLAGS = -g -O2 -target bpf -D__TARGET_ARCH_$(ARCH) \
             -Wall -Wno-unused-value -Wno-pointer-sign -Wno-compare-distinct-pointer-types \
             -I/usr/include -I/usr/include/bpf

OBJS = xdp_dropbox.bpf.o

.PHONY: all clean check

all: $(OBJS)

%.bpf.o: %.bpf.c
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@
	@echo "OK: $@"

# Quick validator — run this locally before deploying to catch obvious bugs.
# Requires the binary to compile and to pass the kernel's BPF verifier.
# bpftool prog load does a real verifier pass without attaching.
check: $(OBJS)
	@echo "== verifier check (requires root + CAP_BPF) =="
	$(BPFTOOL) prog load $< /sys/fs/bpf/_xdp_dropbox_check type xdp \
	    && $(BPFTOOL) prog show pinned /sys/fs/bpf/_xdp_dropbox_check \
	    ; rm -f /sys/fs/bpf/_xdp_dropbox_check

clean:
	rm -f $(OBJS)
