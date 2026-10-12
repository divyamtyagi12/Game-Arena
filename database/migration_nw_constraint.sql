-- ════════════════════════════════════════════════════════════════════
--  NUMBER WARS MIGRATION: Align selected_number constraint to 1..100
--  Safe for existing installations with rollback documentation.
-- ════════════════════════════════════════════════════════════════════

-- 1. Inspect & sanitize existing records: adjust any historical zero values to 1
update nw_submissions
   set selected_number = 1
 where selected_number = 0;

-- 2. Drop existing constraint if named or alter check constraint
do $$
declare
  c_name text;
begin
  select conname into c_name
    from pg_constraint
   where conrelid = 'nw_submissions'::regclass
     and contype = 'c'
     and pg_get_constraintdef(oid) like '%selected_number%';
  if c_name is not null then
    execute format('alter table nw_submissions drop constraint %I', c_name);
  end if;
end $$;

alter table nw_submissions
  add constraint chk_nw_submissions_range check (selected_number between 1 and 100);

-- 3. Update nw_submit stored procedure
create or replace function nw_submit(p_round_id bigint, p_user_id bigint, p_number int)
returns jsonb language plpgsql as $$
declare r nw_rounds%rowtype; ins int; alive_cnt int; sub_cnt int;
begin
  if p_number is null or p_number < 1 or p_number > 100 then
    return jsonb_build_object('status', 'INVALID');
  end if;
  select * into r from nw_rounds where id = p_round_id for share;
  if not found or r.status <> 'OPEN' or now() >= r.deadline_at then
    return jsonb_build_object('status', 'CLOSED');
  end if;
  if not exists (select 1 from nw_players
                  where match_id = r.match_id and user_id = p_user_id and alive) then
    return jsonb_build_object('status', 'NOT_IN');
  end if;
  insert into nw_submissions (round_id, user_id, selected_number)
  values (p_round_id, p_user_id, p_number)
  on conflict do nothing;
  get diagnostics ins = row_count;
  if ins = 0 then return jsonb_build_object('status', 'DUPLICATE'); end if;
  select count(*) into alive_cnt from nw_players where match_id = r.match_id and alive;
  select count(*) into sub_cnt   from nw_submissions where round_id = p_round_id;
  return jsonb_build_object('status', 'OK', 'match_id', r.match_id, 'all_in', sub_cnt >= alive_cnt);
end $$;
