package main

import (
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"testing"
)

func TestEnvelopeRejectsManagementBackendAndOverride(t *testing.T) {
	e := fixtureEnvelope()
	for _, p := range []int{8443, 22, 10080} {
		d := fixtureDesired(e)
		d.Bindings[0].BackendPort = p
		if validateDesired(d, e) == nil {
			t.Fatalf("allowed backend %d", p)
		}
	}
	var d Desired
	if decodeStrict([]byte(`{"backend_ipv4":"127.0.0.1"}`), &d) == nil {
		t.Fatal("accepted destination override")
	}
}
func TestOccupiedBindAndDurableRecovery(t *testing.T) {
	e := fixtureEnvelope()
	s, err := newService(e, t.TempDir(), []byte("test secret"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	l, err := net.ListenPacket("udp4", "0.0.0.0:0")
	if err != nil {
		t.Fatal(err)
	}
	d := fixtureDesired(e)
	d.Bindings[0].PublicPort = l.LocalAddr().(*net.UDPAddr).Port
	if _, err = s.prepare(PrepareRequest{Desired: d}); err == nil {
		t.Fatal("took occupied bind")
	}
	l.Close()
	p, err := s.prepare(PrepareRequest{Desired: d})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.commit(p); err != nil {
		t.Fatal(err)
	}
	s.close()
	recovered, err := newService(e, s.dir, []byte("test secret"))
	if err != nil {
		t.Fatal(err)
	}
	defer recovered.close()
	if recovered.applied == nil || recovered.applied.Revision != 1 {
		t.Fatal("committed plan lost")
	}
}
func TestStagedPlanDoesNotSurviveRestart(t *testing.T) {
	e := fixtureEnvelope()
	s, _ := newService(e, t.TempDir(), []byte("secret"))
	d := fixtureDesired(e)
	p, e2 := s.prepare(PrepareRequest{Desired: d})
	if e2 != nil {
		t.Fatal(e2)
	}
	if p.Revision != 1 {
		t.Fatal(p)
	}
	s.close()
	r, e2 := newService(e, s.dir, []byte("secret"))
	if e2 != nil {
		t.Fatal(e2)
	}
	defer r.close()
	if r.applied != nil || len(r.active) != 0 {
		t.Fatal("staged plan restored")
	}
	if _, e2 = r.prepare(PrepareRequest{Desired: d}); e2 != nil {
		t.Fatal(e2)
	}
}
func TestJournalRejectsEnvelopeMismatch(t *testing.T) {
	e := fixtureEnvelope()
	dir := t.TempDir()
	s, _ := newService(e, dir, []byte("secret"))
	p, err := s.prepare(PrepareRequest{Desired: fixtureDesired(e)})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.commit(p); err != nil {
		t.Fatal(err)
	}
	s.close()
	e.BackendIPv4 = "10.250.0.3"
	if _, err = newService(e, dir, []byte("secret")); err == nil {
		t.Fatal("restored different envelope")
	}
	if _, err = os.Stat(filepath.Join(dir, "journal.json")); err != nil {
		t.Fatal(err)
	}
}
func fixtureEnvelope() Envelope {
	return Envelope{EnvelopeID: "11111111-1111-4111-8111-111111111111", NodeID: "22222222-2222-4222-8222-222222222222", BackendIPv4: "10.250.0.2", BindAddresses: []string{"0.0.0.0", "::"}, ReservedPorts: []int{8443}, MaxBindings: 128, MaxUDPFlows: 4096, MaxTCPConnections: 4096}
}
func fixtureDesired(e Envelope) Desired {
	return Desired{APIVersion: apiVersion, ProtocolVersion: "1.0", NodeID: e.NodeID, Revision: 1, EnvelopeDigest: digest(e), EnabledProtocols: []string{"amneziawg"}, Bindings: []Binding{{BindingID: "33333333-3333-4333-8333-333333333333", DeploymentID: "44444444-4444-4444-8444-444444444444", Transport: "udp", PublicAddress: "0.0.0.0", PublicPort: 30201, BackendRole: "awg", BackendPort: 20001}}}
}

var _ = json.Marshal

func TestValidateDoesNotAdvanceAcceptedOrCreateJournal(t *testing.T) {
	s, err := newService(fixtureEnvelope(), t.TempDir(), []byte("secret"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	d := fixtureDesired(s.envelope)
	if v := s.validate(d); v["valid"] != true {
		t.Fatal(v)
	}
	d.Bindings[0].PublicPort++
	if v := s.validate(d); v["valid"] != true {
		t.Fatal(v)
	}
	if s.accepted != nil || s.pending != nil {
		t.Fatal("dry run mutated revision")
	}
	if _, e := os.Stat(filepath.Join(s.dir, "journal.json")); !os.IsNotExist(e) {
		t.Fatal("dry run persisted")
	}
}
func TestStaleDigestAndTicketCannotReplaceCommittedSockets(t *testing.T) {
	s, err := newService(fixtureEnvelope(), t.TempDir(), []byte("secret"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	d := fixtureDesired(s.envelope)
	p, err := s.prepare(PrepareRequest{Desired: d})
	if err != nil {
		t.Fatal(err)
	}
	wrong := p
	wrong.TicketID = "55555555-5555-4555-8555-555555555555"
	if _, e := s.commit(wrong); e == nil {
		t.Fatal("wrong ticket committed")
	}
	if _, e := s.commit(p); e != nil {
		t.Fatal(e)
	}
	d.Bindings[0].PublicPort++
	if _, e := s.prepare(PrepareRequest{Desired: d}); e == nil {
		t.Fatal("reused revision digest")
	}
	d.Revision = 2
	p2, e := s.prepare(PrepareRequest{Desired: d})
	if e != nil {
		t.Fatal(e)
	}
	if e = s.abort(p2); e != nil {
		t.Fatal(e)
	}
	d.Revision = 1
	d.Bindings[0].PublicPort--
	if _, e = s.prepare(PrepareRequest{Desired: d}); e == nil {
		t.Fatal("lower accepted revision")
	}
	if s.applied.Revision != 1 || len(s.active) != 1 {
		t.Fatal("old commit disrupted")
	}
}
func TestDuplicateJSONAndNullConfigRejected(t *testing.T) {
	var d Desired
	if decodeStrict([]byte(`{"revision":1,"revision":2}`), &d) == nil {
		t.Fatal("duplicate JSON keys")
	}
	s, err := newService(fixtureEnvelope(), t.TempDir(), []byte("secret"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	d = fixtureDesired(s.envelope)
	d.Bindings = nil
	if _, e := s.prepare(PrepareRequest{Desired: d}); e == nil {
		t.Fatal("null full config")
	}
}
func TestIPv6SocketDoesNotTakeIPv4Port(t *testing.T) {
	l, e := net.Listen("tcp4", "0.0.0.0:0")
	if e != nil {
		t.Fatal(e)
	}
	defer l.Close()
	s, err := newService(fixtureEnvelope(), t.TempDir(), []byte("secret"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	d := fixtureDesired(s.envelope)
	d.EnabledProtocols = []string{"trusttunnel"}
	d.Bindings[0].Transport = "tcp"
	d.Bindings[0].BackendRole = "trusttunnel"
	d.Bindings[0].BackendPort = 8444
	d.Bindings[0].PublicAddress = "::"
	d.Bindings[0].PublicPort = l.Addr().(*net.TCPAddr).Port
	p, e := s.prepare(PrepareRequest{Desired: d})
	if e != nil {
		t.Fatal("IPv6only must coexist", e)
	}
	if e = s.abort(p); e != nil {
		t.Fatal(e)
	}
}
func TestCommitRetargetsOwnedPublicSocketOnlyAfterDurableCommit(t *testing.T) {
	s, err := newService(fixtureEnvelope(), t.TempDir(), []byte("secret"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	d := fixtureDesired(s.envelope)
	p, e := s.prepare(PrepareRequest{Desired: d})
	if e != nil {
		t.Fatal(e)
	}
	if _, e = s.commit(p); e != nil {
		t.Fatal(e)
	}
	old := s.active[bindingKey(d.Bindings[0])]
	d.Revision = 2
	d.Bindings[0].BackendPort = 20002
	p, e = s.prepare(PrepareRequest{Desired: d})
	if e != nil {
		t.Fatal("same owned public socket must reserve", e)
	}
	if old.target != "10.250.0.2:20001" {
		t.Fatal("prepare retargeted live socket")
	}
	if _, e = s.commit(p); e != nil {
		t.Fatal(e)
	}
	if s.active[bindingKey(d.Bindings[0])] != old || old.target != "10.250.0.2:20002" {
		t.Fatal("controlled retarget missing")
	}
}
func TestStateDirectoryHasSingleWriter(t *testing.T) {
	dir := t.TempDir()
	s, e := newService(fixtureEnvelope(), dir, []byte("secret"))
	if e != nil {
		t.Fatal(e)
	}
	defer s.close()
	other, e := newService(fixtureEnvelope(), dir, []byte("secret"))
	if e == nil {
		other.close()
		t.Fatal("second journal writer admitted")
	}
	s.close()
	other, e = newService(fixtureEnvelope(), dir, []byte("secret"))
	if e != nil {
		t.Fatal("writer lock not released", e)
	}
	other.close()
}

func TestACMEToTrustTunnelRoleKeepsOwnedListener(t *testing.T) {
	s, err := newService(fixtureEnvelope(), t.TempDir(), []byte("secret"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	d := fixtureDesired(s.envelope)
	d.EnabledProtocols = []string{"trusttunnel"}
	d.Bindings[0].Transport = "tcp"
	d.Bindings[0].BackendRole = "acme-tls-alpn"
	d.Bindings[0].BackendPort = 8444
	d.Bindings[0].PublicPort = 443
	p, err := s.prepare(PrepareRequest{Desired: d})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.commit(p); err != nil {
		t.Fatal(err)
	}
	listener := s.active[bindingKey(d.Bindings[0])]
	d.Revision = 2
	d.Bindings[0].BackendRole = "trusttunnel"
	d.Bindings[0].BindingID = "55555555-5555-4555-8555-555555555555"
	p, err = s.prepare(PrepareRequest{Desired: d})
	if err != nil {
		t.Fatal("owned ACME socket rejected", err)
	}
	if _, err = s.commit(p); err != nil {
		t.Fatal(err)
	}
	if s.active[bindingKey(d.Bindings[0])] != listener {
		t.Fatal("unchanged forwarding socket replaced")
	}
}
