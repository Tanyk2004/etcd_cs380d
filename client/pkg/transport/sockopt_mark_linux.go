//go:build linux

package transport

import "syscall"

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
