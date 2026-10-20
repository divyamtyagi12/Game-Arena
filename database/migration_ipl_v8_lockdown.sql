-- ════════════════════════════════════════════════════════════════════
--  MIGRATION: IPL Security Lockdown (Revoke Public/Anon Access)
--  Ensures all IPL tables and functions are restricted to service_role
-- ════════════════════════════════════════════════════════════════════

do $$
declare r record;
begin
  -- 1. Tables in public
  for r in select tablename from pg_tables where schemaname = 'public' and tablename like 'ipl\_%' loop
    execute format('alter table public.%I enable row level security', r.tablename);
    execute format('revoke all on table public.%I from public, anon, authenticated', r.tablename);
  end loop;

  -- 2. Tables in ipl_private
  for r in select tablename from pg_tables where schemaname = 'ipl_private' loop
    execute format('alter table ipl_private.%I enable row level security', r.tablename);
    execute format('revoke all on table ipl_private.%I from public, anon, authenticated', r.tablename);
  end loop;

  -- 3. Functions in public (ipl_*)
  for r in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
            where n.nspname = 'public' and p.proname like 'ipl\_%' loop
    execute format('revoke all on function %s from public, anon, authenticated', r.sig);
    execute format('grant execute on function %s to service_role', r.sig);
  end loop;
end $$;
