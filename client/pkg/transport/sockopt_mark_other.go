//go:build !linux

package transport

import "syscall"

// makeMarkControl is a no-op stub for non-Linux builds since SO_MARK is a
// Linux-only socket option.
func makeMarkControl(_ uint32) func(network, addr string, conn syscall.RawConn) error {
	return nil
}
