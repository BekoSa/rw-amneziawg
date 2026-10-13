package main

import (
	"io"
	"net"
	"sync"
	"time"
)

type budget struct {
	mu                       sync.Mutex
	tcp, udp, maxTCP, maxUDP int
}

func (b *budget) acquire(udp bool) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	if udp {
		if b.udp >= b.maxUDP {
			return false
		}
		b.udp++
		return true
	}
	if b.tcp >= b.maxTCP {
		return false
	}
	b.tcp++
	return true
}
func (b *budget) release(udp bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if udp {
		b.udp--
	} else {
		b.tcp--
	}
}

type relay struct {
	listener    net.Listener
	packet      net.PacketConn
	target      string
	generation  uint64
	limits      *budget
	idle        time.Duration
	mu          sync.Mutex
	flows       map[string]*net.UDPConn
	connections map[net.Conn]bool
	stopped     bool
	once        sync.Once
}

func newRelay(l net.Listener, p net.PacketConn, target string, maxTCP, maxUDP int, idle time.Duration) *relay {
	return &relay{listener: l, packet: p, target: target, limits: &budget{maxTCP: maxTCP, maxUDP: maxUDP}, idle: idle, flows: make(map[string]*net.UDPConn), connections: make(map[net.Conn]bool)}
}
func (r *relay) activate() {
	r.once.Do(func() {
		if r.listener != nil {
			go r.tcpLoop()
		} else {
			go r.udpLoop()
		}
	})
}
func (r *relay) tcpLoop() {
	for {
		c, e := r.listener.Accept()
		if e != nil {
			return
		}
		if !r.limits.acquire(false) {
			c.Close()
			continue
		}
		r.mu.Lock()
		if r.stopped {
			r.mu.Unlock()
			c.Close()
			r.limits.release(false)
			return
		}
		r.connections[c] = true
		r.mu.Unlock()
		go r.tcp(c)
	}
}

type activityReader struct {
	source net.Conn
	other  net.Conn
	idle   time.Duration
}

func (a activityReader) Read(p []byte) (int, error) {
	n, err := a.source.Read(p)
	if n > 0 {
		deadline := time.Now().Add(a.idle)
		a.source.SetDeadline(deadline)
		a.other.SetDeadline(deadline)
	}
	return n, err
}
func (r *relay) tcp(c net.Conn) {
	defer func() { c.Close(); r.mu.Lock(); delete(r.connections, c); r.mu.Unlock(); r.limits.release(false) }()
	r.mu.Lock()
	target, generation := r.target, r.generation
	r.mu.Unlock()
	b, e := net.DialTimeout("tcp4", target, 5*time.Second)
	if e != nil {
		return
	}
	defer b.Close()
	r.mu.Lock()
	if r.stopped || generation != r.generation {
		r.mu.Unlock()
		return
	}
	r.connections[b] = true
	r.mu.Unlock()
	defer func() { r.mu.Lock(); delete(r.connections, b); r.mu.Unlock() }()
	done := make(chan struct{})
	c.SetDeadline(time.Now().Add(r.idle))
	b.SetDeadline(time.Now().Add(r.idle))
	go func() {
		io.Copy(b, activityReader{c, b, r.idle})
		if t, ok := b.(*net.TCPConn); ok {
			t.CloseWrite()
		}
		close(done)
	}()
	io.Copy(c, activityReader{b, c, r.idle})
	if t, ok := c.(*net.TCPConn); ok {
		t.CloseWrite()
	}
	<-done
}
func (r *relay) udpLoop() {
	buf := make([]byte, 65535)
	for {
		n, src, e := r.packet.ReadFrom(buf)
		if e != nil {
			return
		}
		key := src.String()
		r.mu.Lock()
		c := r.flows[key]
		if c == nil {
			if !r.limits.acquire(true) {
				r.mu.Unlock()
				continue
			}
			a, e := net.ResolveUDPAddr("udp4", r.target)
			if e == nil {
				c, e = net.DialUDP("udp4", nil, a)
			}
			if e != nil {
				r.limits.release(true)
				r.mu.Unlock()
				continue
			}
			r.flows[key] = c
			go r.udpReplies(key, src, c)
		}
		c.SetReadDeadline(time.Now().Add(r.idle))
		r.mu.Unlock()
		c.Write(buf[:n])
	}
}
func (r *relay) udpReplies(key string, src net.Addr, c *net.UDPConn) {
	defer func() {
		c.Close()
		r.mu.Lock()
		if r.flows[key] == c {
			delete(r.flows, key)
		}
		r.mu.Unlock()
		r.limits.release(true)
	}()
	buf := make([]byte, 65535)
	for {
		n, e := c.Read(buf)
		if e != nil {
			return
		}
		c.SetReadDeadline(time.Now().Add(r.idle))
		if _, e = r.packet.WriteTo(buf[:n], src); e != nil {
			return
		}
	}
}
func (r *relay) flowCount() int { r.mu.Lock(); defer r.mu.Unlock(); return len(r.flows) }
func (r *relay) close() {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.stopped {
		return
	}
	r.stopped = true
	if r.listener != nil {
		r.listener.Close()
	}
	if r.packet != nil {
		r.packet.Close()
	}
	for c := range r.connections {
		c.Close()
	}
	for _, c := range r.flows {
		c.Close()
	}
}

// Handoff retains the exclusive listener but revokes flows to the old backend.
// A dial started before commit cannot attach after this generation changes.
func (r *relay) retarget(target string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.target == target {
		return
	}
	r.generation++
	r.target = target
	for c := range r.connections {
		c.Close()
	}
	for _, c := range r.flows {
		c.Close()
	}
	r.flows = make(map[string]*net.UDPConn)
}
