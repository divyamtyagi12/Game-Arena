-- ══════════════════════════════════════════════════════════════════════
-- IPL DRAFT — NON-DESTRUCTIVE BACKUP (run BEFORE any migration)
--
-- Copies every IPL Draft table (public.ipl_* and ipl_private.*) into a NEW schema named
--   ipl_backup_<YYYYMMDD_HH24MISS>
-- and verifies row counts. It never alters, truncates or drops anything that already exists.
-- Works in the Supabase SQL editor (no pg_dump access needed). Anime Draft / Number Wars tables are NOT touched.
--
-- Restore (manual, only if a migration must be undone):  insert into public.<t> select * from <backup_schema>.<t>;
-- Removing an old backup schema is a destructive action and is deliberately NOT done here.
-- ══════════════════════════════════════════════════════════════════════
do $$
declare
  bk text := 'ipl_backup_' || to_char(now() at time zone 'utc', 'YYYYMMDD_HH24MISS');
  r record; n_src bigint; n_dst bigint; total int := 0; bad int := 0;
begin
  execute format('create schema %I', bk);
  for r in
    select table_schema as s, table_name as t
      from information_schema.tables
     where table_type = 'BASE TABLE'
       and ((table_schema = 'public' and table_name like 'ipl\_%') or table_schema = 'ipl_private')
     order by 1, 2
  loop
    execute format('create table %I.%I as table %I.%I', bk, r.s || '__' || r.t, r.s, r.t);
    execute format('select count(*) from %I.%I', r.s, r.t) into n_src;
    execute format('select count(*) from %I.%I', bk, r.s || '__' || r.t) into n_dst;
    total := total + 1;
    if n_src <> n_dst then bad := bad + 1; raise warning 'row-count mismatch for %.%: % vs %', r.s, r.t, n_src, n_dst; end if;
  end loop;
  execute format('comment on schema %I is %L', bk, 'IPL Draft backup taken ' || now()::text || ' — safe to keep; drop only with owner approval');
  if bad > 0 then raise exception 'backup % INCOMPLETE: % of % tables mismatched', bk, bad, total; end if;
  raise notice 'IPL backup OK: schema % (% tables)', bk, total;
end $$;
