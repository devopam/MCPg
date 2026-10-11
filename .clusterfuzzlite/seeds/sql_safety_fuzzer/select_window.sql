SELECT id, row_number() OVER (PARTITION BY g ORDER BY ts DESC) FROM events
