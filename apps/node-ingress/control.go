package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

var uuidPattern = regexp.MustCompile(`^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$`)

func uuid() string {
	b := make([]byte, 16)
	rand.Read(b)
	b[6] = (b[6] & 15) | 64
	b[8] = (b[8] & 63) | 128
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
}
func validAddress(v string) bool {
	a, e := netip.ParseAddr(v)
	return e == nil && !a.IsLoopback() && !a.IsLinkLocalUnicast() && !a.IsMulticast() && !a.Is4In6() && a.Zone() == ""
}
func validateEnvelope(e Envelope) error {
	a, er := netip.ParseAddr(e.BackendIPv4)
	if er != nil || !a.Is4() || !a.IsPrivate() || !uuidPattern.MatchString(e.NodeID) || !uuidPattern.MatchString(e.EnvelopeID) || e.MaxBindings < 1 || e.MaxBindings > 1024 || e.MaxUDPFlows < 1 || e.MaxUDPFlows > 1000000 || e.MaxTCPConnections < 1 || e.MaxTCPConnections > 1000000 || len(e.BindAddresses) < 1 || len(e.BindAddresses) > 32 || len(e.ReservedPorts) > 128 {
		return fmt.Errorf("ENVELOPE_MISMATCH")
	}
	seen := map[string]bool{}
	for _, v := range e.BindAddresses {
		if !validAddress(v) || seen[v] {
			return fmt.Errorf("ENVELOPE_MISMATCH")
		}
		seen[v] = true
	}
	ports := map[int]bool{}
	for _, p := range e.ReservedPorts {
		if p < 1 || p > 65535 || ports[p] {
			return fmt.Errorf("ENVELOPE_MISMATCH")
		}
		ports[p] = true
	}
	return nil
}
func validateDesired(d Desired, e Envelope) error {
	if d.APIVersion != apiVersion || d.ProtocolVersion != "1.0" || d.NodeID != e.NodeID || d.EnvelopeDigest != digest(e) {
		return fmt.Errorf("ENVELOPE_MISMATCH")
	}
	if d.Revision < 1 || len(d.Bindings) > e.MaxBindings || d.Bindings == nil || d.EnabledProtocols == nil || len(d.EnabledProtocols) > 2 {
		return fmt.Errorf("INVALID_REQUEST")
	}
	enabled := map[string]bool{}
	for _, p := range d.EnabledProtocols {
		if (p != "amneziawg" && p != "trusttunnel") || enabled[p] {
			return fmt.Errorf("INVALID_REQUEST")
		}
		enabled[p] = true
	}
	ids := map[string]bool{}
	awg := map[int]string{}
	tt := ""
	for i, b := range d.Bindings {
		if !uuidPattern.MatchString(b.BindingID) || !uuidPattern.MatchString(b.DeploymentID) || ids[b.BindingID] || !validAddress(b.PublicAddress) || b.PublicPort < 1 || b.PublicPort > 65535 {
			return fmt.Errorf("INVALID_REQUEST")
		}
		ids[b.BindingID] = true
		allowed := false
		for _, a := range e.BindAddresses {
			if a == b.PublicAddress {
				allowed = true
			}
		}
		if !allowed {
			return fmt.Errorf("ENVELOPE_MISMATCH")
		}
		for _, p := range e.ReservedPorts {
			if p == b.PublicPort {
				return fmt.Errorf("RESERVED_CONTROL_PORT")
			}
		}
		if b.Transport != "tcp" && b.Transport != "udp" {
			return fmt.Errorf("INVALID_REQUEST")
		}
		switch b.BackendRole {
		case "awg":
			if !enabled["amneziawg"] || b.Transport != "udp" || b.BackendPort < 20000 || b.BackendPort > 29999 {
				return fmt.Errorf("INVALID_REQUEST")
			}
			if owner, ok := awg[b.BackendPort]; ok && owner != b.DeploymentID {
				return fmt.Errorf("INVALID_REQUEST")
			}
			awg[b.BackendPort] = b.DeploymentID
		case "trusttunnel":
			want := 8444
			if b.Transport == "udp" {
				want = 9443
			}
			if !enabled["trusttunnel"] || b.BackendPort != want {
				return fmt.Errorf("INVALID_REQUEST")
			}
		case "acme-http":
			if !enabled["trusttunnel"] || b.Transport != "tcp" || b.PublicPort != 80 || b.BackendPort != 8080 {
				return fmt.Errorf("INVALID_REQUEST")
			}
		case "acme-tls-alpn":
			if !enabled["trusttunnel"] || b.Transport != "tcp" || b.PublicPort != 443 || b.BackendPort != 8444 {
				return fmt.Errorf("INVALID_REQUEST")
			}
		default:
			return fmt.Errorf("INVALID_REQUEST")
		}
		if b.BackendRole != "awg" {
			if tt != "" && tt != b.DeploymentID {
				return fmt.Errorf("INVALID_REQUEST")
			}
			tt = b.DeploymentID
		}
		for _, other := range d.Bindings[:i] {
			a, _ := netip.ParseAddr(b.PublicAddress)
			oa, _ := netip.ParseAddr(other.PublicAddress)
			if b.Transport == other.Transport && b.PublicPort == other.PublicPort && a.Is4() == oa.Is4() && (a == oa || a.IsUnspecified() || oa.IsUnspecified()) {
				return fmt.Errorf("PORT_IN_USE")
			}
		}
	}
	return nil
}

type session struct {
	challenge string
	nonce     uint64
	expires   time.Time
}
type service struct {
	mu                sync.Mutex
	envelope          Envelope
	dir               string
	secret            []byte
	accepted, applied *Desired
	lastCommit        *Prepared
	pending           *Prepared
	desired           *Desired
	active, staged    map[string]*relay
	sessions          map[string]*session
	epoch             string
	limits            *budget
	lockFile          *os.File
}

func newService(e Envelope, dir string, secret []byte) (*service, error) {
	if err := validateEnvelope(e); err != nil {
		return nil, err
	}
	if len(secret) == 0 {
		return nil, fmt.Errorf("AUTH_INVALID")
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	if err := os.Chmod(dir, 0700); err != nil {
		return nil, err
	}
	s := &service{envelope: e, dir: dir, secret: bytes.TrimSpace(secret), active: map[string]*relay{}, staged: map[string]*relay{}, sessions: map[string]*session{}, epoch: uuid(), limits: &budget{maxTCP: e.MaxTCPConnections, maxUDP: e.MaxUDPFlows}}

	lock, err := os.OpenFile(filepath.Join(dir, ".writer.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, fmt.Errorf("INGRESS_UNAVAILABLE")
	}
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		lock.Close()
		return nil, fmt.Errorf("INGRESS_UNAVAILABLE")
	}
	s.lockFile = lock
	opened := false
	defer func() {
		if !opened {
			s.close()
		}
	}()
	b, err := os.ReadFile(filepath.Join(dir, "journal.json"))
	if os.IsNotExist(err) {
		opened = true
		return s, nil
	}
	if err != nil {
		return nil, err
	}
	var j journal
	if err = decodeStrict(b, &j); err != nil {
		return nil, err
	}
	if j.EnvelopeDigest != digest(e) {
		return nil, fmt.Errorf("ENVELOPE_MISMATCH")
	}
	if j.Accepted != nil {
		if err = validateDesired(*j.Accepted, e); err != nil {
			return nil, err
		}
	}
	if j.Applied != nil {
		if err = validateDesired(*j.Applied, e); err != nil {
			return nil, err
		}
		if j.Accepted == nil || j.Accepted.Revision < j.Applied.Revision || j.LastCommit == nil || j.LastCommit.Revision != j.Applied.Revision || j.LastCommit.Digest != digest(*j.Applied) {
			return nil, fmt.Errorf("INVALID_REQUEST")
		}
		r, er := s.reserve(*j.Applied)
		if er != nil {
			return nil, er
		}
		s.active = r
		for _, v := range s.active {
			v.activate()
		}
	}
	s.accepted = j.Accepted
	s.applied = j.Applied
	s.lastCommit = j.LastCommit
	opened = true
	return s, nil
}
func bindingKey(b Binding) string {
	return fmt.Sprintf("%s/%s/%d", b.Transport, b.PublicAddress, b.PublicPort)
}
func (s *service) reserve(d Desired) (map[string]*relay, error) {
	out := map[string]*relay{}
	cleanup := func() {
		for k, r := range out {
			if s.active[k] != r {
				r.close()
			}
		}
	}
	for _, b := range d.Bindings {
		k := bindingKey(b)
		if r := s.active[k]; r != nil {
			out[k] = r
			continue
		}
		target := net.JoinHostPort(s.envelope.BackendIPv4, strconv.Itoa(b.BackendPort))
		a, _ := netip.ParseAddr(b.PublicAddress)
		family := "4"
		if a.Is6() {
			family = "6"
		}
		addr := net.JoinHostPort(b.PublicAddress, strconv.Itoa(b.PublicPort))
		lc := net.ListenConfig{Control: func(network, address string, c syscall.RawConn) error {
			if family != "6" {
				return nil
			}
			var er error
			if e := c.Control(func(fd uintptr) { er = syscall.SetsockoptInt(int(fd), syscall.IPPROTO_IPV6, syscall.IPV6_V6ONLY, 1) }); e != nil {
				return e
			}
			return er
		}}
		var l net.Listener
		var p net.PacketConn
		var err error
		if b.Transport == "tcp" {
			l, err = lc.Listen(context.Background(), "tcp"+family, addr)
		} else {
			p, err = lc.ListenPacket(context.Background(), "udp"+family, addr)
		}
		if err != nil {
			cleanup()
			return nil, fmt.Errorf("PORT_IN_USE")
		}
		r := newRelay(l, p, target, s.envelope.MaxTCPConnections, s.envelope.MaxUDPFlows, 90*time.Second)
		r.limits = s.limits
		out[k] = r
	}
	return out, nil
}
func (s *service) discard() {
	for k, r := range s.staged {
		if s.active[k] != r {
			r.close()
		}
	}
	s.staged = map[string]*relay{}
	s.pending = nil
	s.desired = nil
}
func (s *service) prepare(req PrepareRequest) (Prepared, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	d := req.Desired
	// Own immutable slices; callers cannot mutate an accepted revision.
	if req.Desired.Bindings != nil {
		d.Bindings = append([]Binding{}, req.Desired.Bindings...)
	}
	if req.Desired.EnabledProtocols != nil {
		d.EnabledProtocols = append([]string{}, req.Desired.EnabledProtocols...)
	}
	if er := validateDesired(d, s.envelope); er != nil {
		return Prepared{}, er
	}
	if req.ExpectedAppliedRevision != nil && (s.applied == nil || s.applied.Revision != *req.ExpectedAppliedRevision) {
		return Prepared{}, fmt.Errorf("APPLIED_REVISION_MISMATCH")
	}
	if s.accepted != nil {
		if d.Revision < s.accepted.Revision {
			return Prepared{}, fmt.Errorf("STALE_REVISION")
		}
		if d.Revision == s.accepted.Revision && digest(d) != digest(*s.accepted) {
			return Prepared{}, fmt.Errorf("REVISION_CONFLICT")
		}
	}
	if s.pending != nil && s.pending.Revision == d.Revision && s.pending.Digest == digest(d) {
		return *s.pending, nil
	}
	if s.applied != nil && s.applied.Revision == d.Revision && digest(*s.applied) == digest(d) && s.lastCommit != nil {
		return *s.lastCommit, nil
	}
	s.discard()
	r, er := s.reserve(d)
	if er != nil {
		return Prepared{}, er
	}
	p := Prepared{TicketID: uuid(), Revision: d.Revision, Digest: digest(d)}
	if er = s.persist(journal{EnvelopeDigest: digest(s.envelope), Accepted: &d, Applied: s.applied, LastCommit: s.lastCommit}); er != nil {
		for k, v := range r {
			if s.active[k] != v {
				v.close()
			}
		}
		return Prepared{}, fmt.Errorf("INGRESS_UNAVAILABLE")
	}
	s.accepted = &d
	s.desired = &d
	s.pending = &p
	s.staged = r
	return p, nil
}
func (s *service) commit(p Prepared) (map[string]any, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.lastCommit != nil && *s.lastCommit == p {
		return s.stateLocked(), nil
	}
	if s.pending == nil || *s.pending != p {
		return nil, fmt.Errorf("TICKET_MISMATCH")
	}
	if er := s.persist(journal{EnvelopeDigest: digest(s.envelope), Accepted: s.accepted, Applied: s.desired, LastCommit: &p}); er != nil {
		return nil, fmt.Errorf("INGRESS_UNAVAILABLE")
	}
	for k, r := range s.active {
		if s.staged[k] != r {
			r.close()
		}
	}
	s.active = s.staged
	s.staged = map[string]*relay{}
	s.applied = s.desired
	s.lastCommit = &p
	s.pending = nil
	s.desired = nil
	for _, binding := range s.applied.Bindings {
		s.active[bindingKey(binding)].retarget(net.JoinHostPort(s.envelope.BackendIPv4, strconv.Itoa(binding.BackendPort)))
	}
	for _, r := range s.active {
		r.activate()
	}
	return s.stateLocked(), nil
}
func (s *service) abort(p Prepared) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.pending == nil {
		return nil
	}
	if *s.pending != p {
		return fmt.Errorf("TICKET_MISMATCH")
	}
	s.discard()
	return nil
}
func (s *service) validate(d Desired) map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	issues := []map[string]any{}
	er := validateDesired(d, s.envelope)
	if er == nil {
		r, e := s.reserve(d)
		er = e
		for k, v := range r {
			if s.active[k] != v {
				v.close()
			}
		}
	}
	if er != nil {
		issues = append(issues, map[string]any{"code": er.Error(), "message": "Node listener validation failed", "field": nil})
	}
	return map[string]any{"valid": er == nil, "revision": d.Revision, "digest": digest(d), "issues": issues}
}
func (s *service) stateLocked() map[string]any {
	var desired, accepted, applied any
	var dg any
	bindings := []map[string]any{}
	state := "DRAFT"
	if s.accepted != nil {
		accepted = s.accepted.Revision
		desired = accepted
		state = "APPLYING"
	}
	if s.applied != nil {
		applied = s.applied.Revision
		dg = digest(*s.applied)
		state = "APPLYING"
		for _, b := range s.applied.Bindings {
			bindings = append(bindings, map[string]any{"binding": b, "state": "ACTIVE", "backend_ready": false, "error": nil})
		}
	}
	return map[string]any{"api_version": apiVersion, "protocol_version": "1.0", "node_id": s.envelope.NodeID, "desired_revision": desired, "accepted_revision": accepted, "applied_revision": applied, "last_known_good_revision": nil, "applied_digest": dg, "state": state, "healthy": false, "bindings": bindings, "runtime_epoch": s.epoch, "observed_at": time.Now().UTC().Format(time.RFC3339Nano), "error": nil}
}
func (s *service) close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.discard()
	for _, r := range s.active {
		r.close()
	}
	s.active = map[string]*relay{}
	if s.lockFile != nil {
		s.lockFile.Close()
		s.lockFile = nil
	}
}
func respond(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}
func failure(w http.ResponseWriter, code string) {
	status := 400
	switch code {
	case "AUTH_REQUIRED", "AUTH_INVALID", "SESSION_EXPIRED", "REPLAY":
		status = 401
	case "PORT_IN_USE", "STALE_REVISION", "REVISION_CONFLICT", "APPLIED_REVISION_MISMATCH", "TICKET_MISMATCH":
		status = 409
	case "INGRESS_UNAVAILABLE":
		status = 503
	}
	respond(w, status, map[string]string{"code": code, "message": "Ingress request rejected"})
}
func (s *service) authenticate(r *http.Request, b []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	a := s.sessions[r.Header.Get("X-Ingress-Session")]
	if a == nil || time.Now().After(a.expires) {
		return fmt.Errorf("SESSION_EXPIRED")
	}
	n, e := strconv.ParseUint(r.Header.Get("X-Ingress-Nonce"), 10, 64)
	if e != nil || n == 0 {
		return fmt.Errorf("AUTH_INVALID")
	}
	bodyhash := sha256.Sum256(b)
	canonical := fmt.Sprintf("%s\n%s\n%s\n%s\n%d\n%x", r.Header.Get("X-Ingress-Session"), a.challenge, r.Method, r.URL.Path, n, bodyhash)
	mac := hmac.New(sha256.New, s.secret)
	mac.Write([]byte(canonical))
	got, e := hex.DecodeString(r.Header.Get("X-Ingress-MAC"))
	if e != nil || !hmac.Equal(got, mac.Sum(nil)) {
		return fmt.Errorf("AUTH_INVALID")
	}
	if n <= a.nonce {
		return fmt.Errorf("REPLAY")
	}
	a.nonce = n
	return nil
}
func (s *service) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.RawQuery != "" {
		failure(w, "INVALID_REQUEST")
		return
	}
	if r.Method == "GET" && r.URL.Path == "/v1/ingress/session" {
		s.mu.Lock()
		for k, v := range s.sessions {
			if time.Now().After(v.expires) {
				delete(s.sessions, k)
			}
		}
		if len(s.sessions) >= 128 {
			s.mu.Unlock()
			failure(w, "INGRESS_UNAVAILABLE")
			return
		}
		id := uuid()
		b := make([]byte, 32)
		rand.Read(b)
		challenge := hex.EncodeToString(b)
		s.sessions[id] = &session{challenge: challenge, expires: time.Now().Add(5 * time.Minute)}
		s.mu.Unlock()
		respond(w, 200, map[string]any{"session_id": id, "challenge": challenge, "next_nonce": 1})
		return
	}
	if r.Header.Get("X-Ingress-Session") == "" {
		failure(w, "AUTH_REQUIRED")
		return
	}
	b, e := io.ReadAll(http.MaxBytesReader(w, r.Body, 1024*1024))
	if e != nil {
		failure(w, "INVALID_REQUEST")
		return
	}
	if e = s.authenticate(r, b); e != nil {
		failure(w, e.Error())
		return
	}
	switch {
	case r.Method == "GET" && r.URL.Path == "/v1/ingress/state":
		if len(b) > 0 {
			failure(w, "INVALID_REQUEST")
			return
		}
		s.mu.Lock()
		v := s.stateLocked()
		s.mu.Unlock()
		respond(w, 200, v)
	case r.Method == "POST" && r.URL.Path == "/v1/ingress/validate":
		var d Desired
		if decodeStrict(b, &d) != nil {
			failure(w, "INVALID_REQUEST")
			return
		}
		respond(w, 200, s.validate(d))
	case r.Method == "POST" && r.URL.Path == "/v1/ingress/prepare":
		var q PrepareRequest
		if decodeStrict(b, &q) != nil {
			failure(w, "INVALID_REQUEST")
			return
		}
		v, e := s.prepare(q)
		if e != nil {
			failure(w, e.Error())
			return
		}
		respond(w, 200, v)
	case r.Method == "POST" && (r.URL.Path == "/v1/ingress/commit" || r.URL.Path == "/v1/ingress/abort"):
		var p Prepared
		if decodeStrict(b, &p) != nil || !uuidPattern.MatchString(p.TicketID) || p.Revision < 1 || len(p.Digest) != 64 {
			failure(w, "INVALID_REQUEST")
			return
		}
		if strings.HasSuffix(r.URL.Path, "abort") {
			if e = s.abort(p); e != nil {
				failure(w, e.Error())
				return
			}
			w.WriteHeader(204)
			return
		}
		v, e := s.commit(p)
		if e != nil {
			failure(w, e.Error())
			return
		}
		respond(w, 200, v)
	default:
		failure(w, "INVALID_REQUEST")
	}
}
