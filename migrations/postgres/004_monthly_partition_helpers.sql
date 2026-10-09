CREATE OR REPLACE FUNCTION ensure_domain_event_month(p_month date)
RETURNS text LANGUAGE plpgsql AS $$
DECLARE start_date date := date_trunc('month',p_month)::date;
DECLARE end_date date := (date_trunc('month',p_month)+interval '1 month')::date;
DECLARE partition_name text := 'domain_event_'||to_char(start_date,'YYYYMM');
BEGIN
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF domain_event FOR VALUES FROM (%L) TO (%L)',partition_name,start_date,end_date);
  RETURN partition_name;
END $$;
