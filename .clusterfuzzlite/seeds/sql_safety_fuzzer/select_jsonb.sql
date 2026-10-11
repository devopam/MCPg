SELECT data->'k'->>'n', jsonb_array_length(data->'arr') FROM docs WHERE data @> '{"a":1}'
