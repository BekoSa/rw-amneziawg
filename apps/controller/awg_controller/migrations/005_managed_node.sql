CREATE TABLE node_resources (
 node_id uuid PRIMARY KEY REFERENCES awg_nodes(id) ON DELETE CASCADE, capabilities jsonb,
 desired jsonb, actual jsonb, enabled_protocols jsonb,
 next_revision bigint NOT NULL DEFAULT 1,
 last_seen_at timestamptz, last_error_code text
);
CREATE TABLE node_resource_revisions (
 node_id uuid NOT NULL REFERENCES awg_nodes(id) ON DELETE CASCADE, revision bigint NOT NULL,
 digest text NOT NULL, desired jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(node_id,revision)
);
CREATE TABLE node_awg_backends (
 node_id uuid NOT NULL REFERENCES awg_nodes(id) ON DELETE CASCADE,
 deployment_id uuid PRIMARY KEY REFERENCES awg_deployments(id) ON DELETE CASCADE,
 port integer NOT NULL CHECK (port BETWEEN 20000 AND 29999), UNIQUE(node_id,port)
);
INSERT INTO awg_schema_migrations(version) VALUES(5) ON CONFLICT DO NOTHING;
