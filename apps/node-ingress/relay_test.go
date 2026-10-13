package main

import (
	"io"
	"net"
	"testing"
	"time"
)

func TestTCPHalfClosePreservesBackendReply(t *testing.T) {
	b, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer b.Close()
	go func() {
		c, _ := b.Accept()
		defer c.Close()
		v, _ := io.ReadAll(c)
		c.Write(append([]byte("reply:"), v...))
	}()
	l, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	r := newRelay(l, nil, b.Addr().String(), 8, 8, time.Second)
	defer r.close()
	r.activate()
	c, e := net.DialTCP("tcp4", nil, l.Addr().(*net.TCPAddr))
	if e != nil {
		t.Fatal(e)
	}
	defer c.Close()
	c.SetDeadline(time.Now().Add(3 * time.Second))
	c.Write([]byte("payload"))
	c.CloseWrite()
	v, e := io.ReadAll(c)
	if e != nil || string(v) != "reply:payload" {
		t.Fatalf("reply %q err %v", v, e)
	}
}
func TestUDPFlowsAreIsolatedAndExpire(t *testing.T) {
	b, e := net.ListenPacket("udp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer b.Close()
	go func() {
		v := make([]byte, 65535)
		for {
			n, a, e := b.ReadFrom(v)
			if e != nil {
				return
			}
			b.WriteTo(v[:n], a)
		}
	}()
	p, e := net.ListenPacket("udp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	r := newRelay(nil, p, b.LocalAddr().String(), 8, 2, 100*time.Millisecond)
	defer r.close()
	r.activate()
	for _, w := range []string{"alice", "bob"} {
		c, e := net.Dial("udp4", p.LocalAddr().String())
		if e != nil {
			t.Fatal(e)
		}
		defer c.Close()
		c.SetDeadline(time.Now().Add(time.Second))
		c.Write([]byte(w))
		v := make([]byte, 30)
		n, e := c.Read(v)
		if e != nil || string(v[:n]) != w {
			t.Fatalf("%q %v", v[:n], e)
		}
	}
	time.Sleep(300 * time.Millisecond)
	if n := r.flowCount(); n != 0 {
		t.Fatalf("leaked %d UDP flows", n)
	}
}
func TestUDPFlowLimitDropsNewSourceAndIdleReleasesBudget(t *testing.T) {
	b, e := net.ListenPacket("udp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer b.Close()
	go func() {
		v := make([]byte, 65535)
		for {
			n, a, e := b.ReadFrom(v)
			if e != nil {
				return
			}
			b.WriteTo(v[:n], a)
		}
	}()
	p, e := net.ListenPacket("udp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	r := newRelay(nil, p, b.LocalAddr().String(), 1, 1, 150*time.Millisecond)
	defer r.close()
	r.activate()
	a, _ := net.Dial("udp4", p.LocalAddr().String())
	defer a.Close()
	c, _ := net.Dial("udp4", p.LocalAddr().String())
	defer c.Close()
	a.SetDeadline(time.Now().Add(time.Second))
	a.Write([]byte("a"))
	v := make([]byte, 5)
	if _, e = a.Read(v); e != nil {
		t.Fatal(e)
	}
	c.SetDeadline(time.Now().Add(50 * time.Millisecond))
	c.Write([]byte("blocked"))
	if _, e = c.Read(v); e == nil {
		t.Fatal("flow limit exceeded")
	}
	time.Sleep(200 * time.Millisecond)
	c.SetDeadline(time.Now().Add(time.Second))
	c.Write([]byte("c"))
	n, e := c.Read(v)
	if e != nil || string(v[:n]) != "c" {
		t.Fatalf("budget not released %q %v", v[:n], e)
	}
}
func TestTCPConnectionLimitClosesExcessAndReleasesAfterClose(t *testing.T) {
	b, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer b.Close()
	accepted := make(chan net.Conn, 3)
	go func() {
		for {
			c, e := b.Accept()
			if e != nil {
				return
			}
			accepted <- c
			go func() { io.Copy(c, c); c.Close() }()
		}
	}()
	l, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	r := newRelay(l, nil, b.Addr().String(), 1, 1, time.Second)
	defer r.close()
	r.activate()
	a, e := net.Dial("tcp4", l.Addr().String())
	if e != nil {
		t.Fatal(e)
	}
	defer a.Close()
	select {
	case <-accepted:
	case <-time.After(5 * time.Second):
		t.Fatal("backend not connected")
	}
	c, _ := net.Dial("tcp4", l.Addr().String())
	defer c.Close()
	c.SetDeadline(time.Now().Add(time.Second))
	v := make([]byte, 1)
	if _, e = c.Read(v); e == nil {
		t.Fatal("excess connection remained")
	}
	a.Close()
	time.Sleep(100 * time.Millisecond)
	c2, e := net.Dial("tcp4", l.Addr().String())
	if e != nil {
		t.Fatal(e)
	}
	defer c2.Close()
	c2.SetDeadline(time.Now().Add(time.Second))
	c2.Write([]byte("z"))
	if _, e = c2.Read(v); e != nil || v[0] != 'z' {
		t.Fatal("TCP budget not released", e)
	}
}
func TestUDPConcurrentRepliesNeverCrossClients(t *testing.T) {
	b, e := net.ListenPacket("udp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer b.Close()
	go func() {
		type message struct {
			value  []byte
			source net.Addr
		}
		messages := []message{}
		for i := 0; i < 2; i++ {
			v := make([]byte, 128)
			n, a, e := b.ReadFrom(v)
			if e != nil {
				return
			}
			messages = append(messages, message{v[:n], a})
		}
		for i := 1; i >= 0; i-- {
			b.WriteTo(messages[i].value, messages[i].source)
		}
	}()
	p, e := net.ListenPacket("udp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	r := newRelay(nil, p, b.LocalAddr().String(), 2, 2, time.Second)
	defer r.close()
	r.activate()
	a, _ := net.Dial("udp4", p.LocalAddr().String())
	defer a.Close()
	c, _ := net.Dial("udp4", p.LocalAddr().String())
	defer c.Close()
	a.SetDeadline(time.Now().Add(time.Second))
	c.SetDeadline(time.Now().Add(time.Second))
	a.Write([]byte("alice-only"))
	c.Write([]byte("bob-only"))
	for _, v := range []struct {
		client net.Conn
		want   string
	}{{a, "alice-only"}, {c, "bob-only"}} {
		buf := make([]byte, 128)
		n, e := v.client.Read(buf)
		if e != nil || string(buf[:n]) != v.want {
			t.Fatalf("crossed client reply %q want %q error %v", buf[:n], v.want, e)
		}
	}
}
func TestTCPIdleTimeoutReleasesSilentConnection(t *testing.T) {
	b, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	defer b.Close()
	go func() {
		c, e := b.Accept()
		if e != nil {
			return
		}
		defer c.Close()
		io.Copy(c, c)
	}()
	l, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	r := newRelay(l, nil, b.Addr().String(), 1, 1, 50*time.Millisecond)
	defer r.close()
	r.activate()
	c, e := net.Dial("tcp4", l.Addr().String())
	if e != nil {
		t.Fatal(e)
	}
	defer c.Close()
	c.SetReadDeadline(time.Now().Add(time.Second))
	v := make([]byte, 1)
	_, e = c.Read(v)
	if timeout, ok := e.(net.Error); e == nil || (ok && timeout.Timeout()) {
		t.Fatal("silent connection did not expire", e)
	}
}
