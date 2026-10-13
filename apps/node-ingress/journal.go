package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

const apiVersion = "node.remnawave-extension.io/v1"

type Envelope struct {
	EnvelopeID        string   `json:"envelope_id"`
	NodeID            string   `json:"node_id"`
	BackendIPv4       string   `json:"backend_ipv4"`
	BindAddresses     []string `json:"bind_addresses"`
	ReservedPorts     []int    `json:"reserved_ports"`
	MaxBindings       int      `json:"max_bindings"`
	MaxUDPFlows       int      `json:"max_udp_flows"`
	MaxTCPConnections int      `json:"max_tcp_connections"`
}
type Binding struct {
	BindingID     string `json:"binding_id"`
	DeploymentID  string `json:"deployment_id"`
	Transport     string `json:"transport"`
	PublicAddress string `json:"public_address"`
	PublicPort    int    `json:"public_port"`
	BackendRole   string `json:"backend_role"`
	BackendPort   int    `json:"backend_port"`
}
type Desired struct {
	APIVersion       string    `json:"api_version"`
	ProtocolVersion  string    `json:"protocol_version"`
	NodeID           string    `json:"node_id"`
	Revision         int       `json:"revision"`
	EnvelopeDigest   string    `json:"envelope_digest"`
	EnabledProtocols []string  `json:"enabled_protocols"`
	Bindings         []Binding `json:"bindings"`
}
type PrepareRequest struct {
	Desired                 Desired `json:"desired"`
	ExpectedAppliedRevision *int    `json:"expected_applied_revision"`
}
type Prepared struct {
	TicketID string `json:"ticket_id"`
	Revision int    `json:"revision"`
	Digest   string `json:"digest"`
}
type journal struct {
	EnvelopeDigest string    `json:"envelope_digest"`
	Accepted       *Desired  `json:"accepted"`
	Applied        *Desired  `json:"applied"`
	LastCommit     *Prepared `json:"last_commit"`
}

func digest(v any) string {
	b, _ := json.Marshal(v)
	var canonical any
	json.Unmarshal(b, &canonical)
	b, _ = json.Marshal(canonical)
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}
func decodeStrict(b []byte, v any) error {
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if e := d.Decode(v); e != nil {
		return fmt.Errorf("INVALID_REQUEST")
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		return fmt.Errorf("INVALID_REQUEST")
	}
	return rejectDuplicateKeys(b)
}
func rejectDuplicateKeys(b []byte) error {
	d := json.NewDecoder(bytes.NewReader(b))
	var walk func() error
	walk = func() error {
		v, e := d.Token()
		if e != nil {
			return e
		}
		if delim, ok := v.(json.Delim); ok {
			switch delim {
			case '{':
				seen := map[string]bool{}
				for d.More() {
					key, e := d.Token()
					if e != nil {
						return e
					}
					s := key.(string)
					if seen[s] {
						return fmt.Errorf("INVALID_REQUEST")
					}
					seen[s] = true
					if e = walk(); e != nil {
						return e
					}
				}
				_, e = d.Token()
				return e
			case '[':
				for d.More() {
					if e = walk(); e != nil {
						return e
					}
				}
				_, e = d.Token()
				return e
			}
		}
		return nil
	}
	return walk()
}
func (s *service) persist(j journal) error {
	b, e := json.Marshal(j)
	if e != nil {
		return e
	}
	tmp, e := os.CreateTemp(s.dir, ".journal-*")
	if e != nil {
		return e
	}
	name := tmp.Name()
	defer os.Remove(name)
	if e = tmp.Chmod(0600); e == nil {
		_, e = tmp.Write(b)
	}
	if e == nil {
		e = tmp.Sync()
	}
	ce := tmp.Close()
	if e == nil {
		e = ce
	}
	if e == nil {
		e = os.Rename(name, filepath.Join(s.dir, "journal.json"))
	}
	if e == nil {
		d, de := os.Open(s.dir)
		if de != nil {
			return de
		}
		e = d.Sync()
		d.Close()
	}
	return e
}
