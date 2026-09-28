CREATE TABLE IF NOT EXISTS awg_schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS awg_nodes (
 id uuid PRIMARY KEY, registration jsonb NOT NULL, capabilities jsonb NOT NULL,
 online boolean NOT NULL DEFAULT false, last_seen_at timestamptz
);
CREATE TABLE IF NOT EXISTS awg_profiles (
 id uuid PRIMARY KEY, draft jsonb NOT NULL, active jsonb, validated_digest text,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS awg_users (
 id uuid PRIMARY KEY, upstream jsonb NOT NULL, encrypted_key text NOT NULL, public_key text NOT NULL UNIQUE,
 key_generation integer NOT NULL DEFAULT 1, deleted boolean NOT NULL DEFAULT false,
 usage numeric(30,0) NOT NULL DEFAULT 0, reset_at timestamptz
);
CREATE TABLE IF NOT EXISTS awg_deployments (
 id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES awg_profiles(id), node_id uuid NOT NULL REFERENCES awg_nodes(id),
 desired jsonb, actual jsonb, next_revision bigint NOT NULL DEFAULT 1, last_seen_at timestamptz,
 UNIQUE(profile_id,node_id)
);
CREATE TABLE IF NOT EXISTS awg_peers (
 id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES awg_users(id), deployment_id uuid NOT NULL REFERENCES awg_deployments(id),
 present boolean NOT NULL DEFAULT true, ipv4 text NOT NULL, ipv6 text,
 rx_total numeric(30,0) NOT NULL DEFAULT 0, tx_total numeric(30,0) NOT NULL DEFAULT 0, latest_handshake timestamptz,
 UNIQUE(user_id,deployment_id)
);
CREATE TABLE IF NOT EXISTS awg_ip_allocations (
 profile_id uuid NOT NULL REFERENCES awg_profiles(id), address inet NOT NULL,
 peer_id uuid REFERENCES awg_peers(id) DEFERRABLE INITIALLY DEFERRED,
 release_after timestamptz, PRIMARY KEY(profile_id,address)
);
CREATE TABLE IF NOT EXISTS awg_tombstones (
 peer_id uuid PRIMARY KEY REFERENCES awg_peers(id), public_key text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 acknowledged_at timestamptz
);
CREATE TABLE IF NOT EXISTS awg_revisions (
 deployment_id uuid NOT NULL REFERENCES awg_deployments(id), revision bigint NOT NULL,
 digest text NOT NULL, desired jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(deployment_id,revision)
);
CREATE TABLE IF NOT EXISTS awg_counter_epochs (
 deployment_id uuid NOT NULL REFERENCES awg_deployments(id), epoch uuid NOT NULL,
 sequence bigint NOT NULL, retired boolean NOT NULL DEFAULT false,
 PRIMARY KEY(deployment_id,epoch)
);
CREATE TABLE IF NOT EXISTS awg_peer_counters (
 peer_id uuid NOT NULL REFERENCES awg_peers(id), epoch uuid NOT NULL, generation integer NOT NULL,
 rx numeric(30,0) NOT NULL, tx numeric(30,0) NOT NULL,
 PRIMARY KEY(peer_id,epoch,generation)
);
CREATE TABLE IF NOT EXISTS awg_errors (
 id bigserial PRIMARY KEY, code text NOT NULL, entity_id uuid, created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO awg_schema_migrations(version) VALUES(1) ON CONFLICT DO NOTHING;
CREATE UNIQUE INDEX IF NOT EXISTS awg_ip_peer_family ON awg_ip_allocations(peer_id,(family(address))) WHERE peer_id IS NOT NULL;
