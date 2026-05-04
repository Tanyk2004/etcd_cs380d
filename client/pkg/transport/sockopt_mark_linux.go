//go:build linux

package transport

import (
	"net"
	"syscall"
)

// makeMarkControl returns a net.Dialer Control function that sets SO_MARK on
// the socket before it connects. We use this to tag stream connections that
// carry Raft heartbeat traffic so the TC BPF program can identify them by mark
// instead of having to inspect packet payloads.
// Note: needs CAP_NET_ADMIN, but we just silently ignore the error if it fails
// so the transport still works without the priority boost.
func makeMarkControl(mark uint32) func(network, addr string, conn syscall.RawConn) error {
	return func(network, addr string, conn syscall.RawConn) error {
		return conn.Control(func(fd uintptr) {
			syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_MARK, int(mark))
		})
	}
}

// SetMarkOnConn sets SO_MARK on an already-accepted net.Conn. This is used to
// mark the server-side (accepted) socket so that outgoing heartbeat writes are
// also tagged for TC BPF priority boosting - the dialer-side mark alone only
// covers the client's outbound connection-setup and probe traffic.
// Silently ignores errors (e.g. missing CAP_NET_ADMIN) so the transport works
// normally without priority boost.
func SetMarkOnConn(conn net.Conn, mark uint32) {
	sc, ok := conn.(syscall.Conn)
	if !ok {
		return
	}
	raw, err := sc.SyscallConn()
	if err != nil {
		return
	}
	raw.Control(func(fd uintptr) {
		syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_MARK, int(mark))
	})
}
