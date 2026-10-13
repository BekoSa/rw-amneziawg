package main

import (
	"bytes"
	"context"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"
)

func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
func run() error {
	b, e := os.ReadFile(env("INGRESS_ENVELOPE_FILE", "/etc/node/envelope.json"))
	if e != nil {
		return fmt.Errorf("envelope unavailable")
	}
	envelope := Envelope{BindAddresses: []string{"0.0.0.0", "::"}, ReservedPorts: []int{8443}, MaxBindings: 128, MaxUDPFlows: 4096, MaxTCPConnections: 4096}
	if decodeStrict(b, &envelope) != nil {
		return fmt.Errorf("invalid envelope")
	}
	secret, e := os.ReadFile(env("INGRESS_SECRET_FILE", "/run/node/ingress.secret"))
	if e != nil || len(bytes.TrimSpace(secret)) < 32 {
		return fmt.Errorf("invalid ingress secret")
	}
	s, e := newService(envelope, env("INGRESS_STATE_DIR", "/var/lib/node-ingress"), secret)
	if e != nil {
		return fmt.Errorf("ingress recovery failed")
	}
	defer s.close()
	path := env("INGRESS_SOCKET", "/run/node/ingress.sock")
	if e = os.MkdirAll(filepath.Dir(path), 0700); e != nil {
		return e
	}
	if info, er := os.Lstat(path); er == nil {
		if info.Mode()&os.ModeSocket == 0 {
			return fmt.Errorf("socket path occupied")
		}
		c, er := net.DialTimeout("unix", path, time.Second)
		if er == nil {
			c.Close()
			return fmt.Errorf("control socket in use")
		}
		if er = os.Remove(path); er != nil {
			return er
		}
	}
	l, e := net.Listen("unix", path)
	if e != nil {
		return e
	}
	defer os.Remove(path)
	defer l.Close()
	if e = os.Chmod(path, 0600); e != nil {
		return e
	}
	server := &http.Server{Handler: s, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 15 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 8192}
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(signals)
	go func() {
		<-signals
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		server.Shutdown(ctx)
	}()
	e = server.Serve(l)
	if e == http.ErrServerClosed {
		return nil
	}
	return e
}
func main() {
	if len(os.Args) == 2 && os.Args[1] == "--healthcheck" {
		c, e := net.DialTimeout("unix", env("INGRESS_SOCKET", "/run/node/ingress.sock"), time.Second)
		if e != nil {
			os.Exit(1)
		}
		c.Close()
		return
	}
	if e := run(); e != nil {
		fmt.Fprintln(os.Stderr, "node ingress unavailable")
		os.Exit(1)
	}
}
