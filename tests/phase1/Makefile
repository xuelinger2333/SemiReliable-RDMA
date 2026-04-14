CC      = gcc
CFLAGS  = -Wall -Wextra -O2 -std=gnu11
LDFLAGS = -libverbs -lpthread -lrt

TESTS   = test_uc_write_imm test_ghost_gradient test_wqe_rate test_netem_loss
COMMON  = rdma_common.h

.PHONY: all clean

all: $(TESTS)

test_uc_write_imm: test_uc_write_imm.c $(COMMON)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

test_ghost_gradient: test_ghost_gradient.c $(COMMON)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

test_wqe_rate: test_wqe_rate.c $(COMMON)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

test_netem_loss: test_netem_loss.c $(COMMON)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f $(TESTS)
