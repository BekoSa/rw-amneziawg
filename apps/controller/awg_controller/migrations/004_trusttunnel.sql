-- Extension-owned TrustTunnel data only. No IPAM or additional user identity.
CREATE TABLE IF NOT EXISTS tt_profiles (
 id uuid PRIMARY KEY, draft jsonb NOT NULL, active jsonb, validated_digest text,
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS tt_credentials (
 user_id uuid PRIMARY KEY REFERENCES awg_users(id), username text NOT NULL UNIQUE,
 encrypted_password text NOT NULL, credential_generation integer NOT NULL DEFAULT 1
 CHECK (credential_generation >= 1)
);
CREATE TABLE IF NOT EXISTS tt_deployments (
 id uuid PRIMARY KEY, profile_id uuid NOT NULL REFERENCES tt_profiles(id),
 node_id uuid NOT NULL UNIQUE REFERENCES awg_nodes(id),
 encrypted_desired text, desired_digest text, actual jsonb,
 next_revision bigint NOT NULL DEFAULT 1 CHECK (next_revision >= 1),
 last_seen_at timestamptz, last_error_code text,
 UNIQUE(profile_id,node_id)
);
CREATE TABLE IF NOT EXISTS tt_peers (
 id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES awg_users(id),
 deployment_id uuid NOT NULL REFERENCES tt_deployments(id), present boolean NOT NULL DEFAULT true,
 credential_generation integer NOT NULL DEFAULT 1,
 UNIQUE(user_id,deployment_id)
);
CREATE TABLE IF NOT EXISTS tt_tombstones (
 peer_id uuid PRIMARY KEY REFERENCES tt_peers(id), credential_generation integer NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), acknowledged_at timestamptz
);
CREATE TABLE IF NOT EXISTS tt_revisions (
 deployment_id uuid NOT NULL REFERENCES tt_deployments(id), revision bigint NOT NULL,
 digest text NOT NULL, encrypted_desired text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(deployment_id,revision)
);
INSERT INTO awg_schema_migrations(version) VALUES(4) ON CONFLICT DO NOTHING;
