package main

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"testing"
)

func TestAuthNonceReplayRejectedAndUnknownFieldsRejected(t *testing.T) {
	s, err := newService(fixtureEnvelope(), t.TempDir(), []byte("secret"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	w := httptest.NewRecorder()
	s.ServeHTTP(w, httptest.NewRequest("GET", "/v1/ingress/session", nil))
	var a map[string]any
	json.Unmarshal(w.Body.Bytes(), &a)
	if w.Code != 200 {
		t.Fatal(w.Code)
	}
	body := []byte(`{"unknown":1}`)
	req := httptest.NewRequest("POST", "/v1/ingress/prepare", bytes.NewReader(body))
	req.Header.Set("X-Ingress-Session", a["session_id"].(string))
	req.Header.Set("X-Ingress-Nonce", "1")
	hash := sha256.Sum256(body)
	canonical := fmt.Sprintf("%s\n%s\nPOST\n/v1/ingress/prepare\n1\n%x", a["session_id"], a["challenge"], hash)
	m := hmac.New(sha256.New, []byte("secret"))
	m.Write([]byte(canonical))
	req.Header.Set("X-Ingress-MAC", hex.EncodeToString(m.Sum(nil)))
	w = httptest.NewRecorder()
	s.ServeHTTP(w, req)
	if w.Code != 400 {
		t.Fatalf("unknown field status %d %s", w.Code, w.Body.String())
	}
	req = httptest.NewRequest("POST", "/v1/ingress/prepare", bytes.NewReader(body))
	req.Header.Set("X-Ingress-Session", a["session_id"].(string))
	req.Header.Set("X-Ingress-Nonce", "1")
	req.Header.Set("X-Ingress-MAC", hex.EncodeToString(m.Sum(nil)))
	w = httptest.NewRecorder()
	s.ServeHTTP(w, req)
	if w.Code != 401 {
		t.Fatal("replay accepted", w.Code)
	}
}
func TestSessionBoundedAndUnauthenticatedNoMutation(t *testing.T) {
	s, err := newService(fixtureEnvelope(), t.TempDir(), []byte("secret"))
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	w := httptest.NewRecorder()
	s.ServeHTTP(w, httptest.NewRequest("POST", "/v1/ingress/commit", bytes.NewReader([]byte(`{}`))))
	if w.Code != 401 || s.applied != nil {
		t.Fatal("unauthenticated mutation")
	}
	for i := 0; i < 300; i++ {
		w = httptest.NewRecorder()
		s.ServeHTTP(w, httptest.NewRequest("GET", "/v1/ingress/session", nil))
	}
	if len(s.sessions) > 128 {
		t.Fatal("unbounded sessions")
	}
}
