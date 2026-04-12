# Build the TC BPF program for Raft heartbeat priority boosting.
# Requires clang + llvm-strip and libbpf headers (apt install libbpf-dev).
#
# make                   - compile tc_prio.bpf.o
# make attach IFACE=eth0 - manually attach to an interface (for testing)
# make detach IFACE=eth0 - remove it
# make setup-qdisc IFACE=eth0 - install PRIO root qdisc so priority actually matters
#
# Note: the ebpf-controller handles attach/detach automatically via TCX,
# so the attach/detach targets here are just for manual debugging.

CLANG ?= clang
STRIP ?= llvm-strip
TC    ?= tc

INCLUDES := -I/usr/include -I/usr/include/bpf -I/usr/include/x86_64-linux-gnu
CFLAGS   := -O2 -Wall -target bpf -D__TARGET_ARCH_x86_64 $(INCLUDES)

OBJ := tc_prio.bpf.o
SRC := tc_prio.bpf.c

.PHONY: all clean attach detach setup-qdisc

all: $(OBJ)

$(OBJ): $(SRC)
	$(CLANG) $(CFLAGS) -c $< -o $@
	$(STRIP) -g $@

clean:
	rm -f $(OBJ)

attach: $(OBJ)
ifndef IFACE
	$(error set IFACE, e.g. make attach IFACE=eth0)
endif
	$(TC) qdisc add dev $(IFACE) clsact 2>/dev/null || true
	$(TC) filter add dev $(IFACE) egress bpf da obj $(OBJ) sec tc

detach:
ifndef IFACE
	$(error set IFACE, e.g. make detach IFACE=eth0)
endif
	$(TC) filter del dev $(IFACE) egress 2>/dev/null || true
	$(TC) qdisc del dev $(IFACE) clsact 2>/dev/null || true

# need this so that priority 7 actually goes to the highest-priority band
setup-qdisc:
ifndef IFACE
	$(error set IFACE)
endif
	$(TC) qdisc replace dev $(IFACE) root prio

