ALTER TABLE awg_peer_counters ADD COLUMN IF NOT EXISTS counter_epoch uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000';
ALTER TABLE awg_peer_counters DROP CONSTRAINT awg_peer_counters_pkey;
ALTER TABLE awg_peer_counters ADD PRIMARY KEY(peer_id,epoch,generation,counter_epoch);
ALTER TABLE awg_counter_epochs ADD COLUMN IF NOT EXISTS observed_at timestamptz NOT NULL DEFAULT '1970-01-01';
CREATE TABLE awg_revoked_keys (
 deployment_id uuid NOT NULL REFERENCES awg_deployments(id), public_key text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(deployment_id,public_key)
);
INSERT INTO awg_schema_migrations(version) VALUES(2);
