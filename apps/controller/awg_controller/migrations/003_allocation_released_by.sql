-- Quarantined addresses remember which peer released them, so only that peer may reclaim them.
ALTER TABLE awg_ip_allocations ADD COLUMN IF NOT EXISTS released_by uuid;
INSERT INTO awg_schema_migrations(version) VALUES(3);
