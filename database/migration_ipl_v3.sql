-- ══════════════════════════════════════════════════════════════════════
-- IPL DRAFT MIGRATION V3   (apply AFTER ipl_draft.sql and migration_ipl_v2.sql)
--
--   0. TAKE A BACKUP FIRST:  database/backup_ipl_tables.sql   (non-destructive; copies every IPL table)
--   1. Persistent batting-order deadline (restart-safe) + race-free batting-order functions
--   2. ipl_finish_drafting(): the explicit DRAFTING -> SYSTEM_TEAM_GENERATION step (the worker used to loop forever)
--   3. Cross-team drafting: a cricketer may sit in several squads of one tournament; uniqueness is now
--      (team_id, player_id) = "no duplicate inside the same XI". Stats are keyed by tournament + team + player.
--   4. Match awards carry the team (pom_team_id / best_bowler_team_id)
--
-- Idempotent and ADDITIVE: no table is dropped or truncated, no rows are deleted. The only constraint changes relax
-- three global (tournament, player) uniques into per-team uniques. Safe to re-run.
-- ══════════════════════════════════════════════════════════════════════

alter table ipl_team_rosters      add column if not exists batting_order smallint check (batting_order between 1 and 11);
alter table ipl_tournament_teams  add column if not exists batting_order_confirmed boolean not null default false;
alter table ipl_tournament_teams  add column if not exists batting_order_deadline_at timestamptz;
alter table ipl_tournament_teams  add column if not exists batting_order_message_id bigint;
alter table ipl_tournament_teams  add column if not exists batting_order_prompted_at timestamptz;
alter table ipl_matches           add column if not exists pom_team_id bigint references ipl_tournament_teams(id);
alter table ipl_matches           add column if not exists best_bowler_team_id bigint references ipl_tournament_teams(id);

-- back-fill the team of historic awards (before v3 a cricketer belonged to exactly one team, so this is exact)
update ipl_matches m set pom_team_id = (select r.team_id from ipl_team_rosters r
                                         where r.tournament_id = m.tournament_id and r.player_id = m.pom_player_id limit 1)
 where m.pom_team_id is null and m.pom_player_id is not null;
update ipl_matches m set best_bowler_team_id = (select r.team_id from ipl_team_rosters r
                                                 where r.tournament_id = m.tournament_id and r.player_id = m.best_bowler_id limit 1)
 where m.best_bowler_team_id is null and m.best_bowler_id is not null;

-- uniqueness: per squad, not per tournament
do $$
declare r record;
begin
  for r in
    select c.conname, c.conrelid::regclass::text as tbl
      from pg_constraint c
     where c.contype = 'u'
       and c.conrelid in ('ipl_team_rosters'::regclass, 'ipl_draft_picks'::regclass, 'ipl_player_reservations'::regclass)
       and (select array_agg(a.attname::text order by a.attname) from pg_attribute a
             where a.attrelid = c.conrelid and a.attnum = any (c.conkey)) = array['player_id', 'tournament_id']
  loop
    execute format('alter table %s drop constraint %I', r.tbl, r.conname);
  end loop;
end $$;
create unique index if not exists uq_ipl_roster_team_player on ipl_team_rosters (team_id, player_id);
create unique index if not exists uq_ipl_picks_team_player  on ipl_draft_picks  (team_id, player_id);
create unique index if not exists uq_ipl_res_offer_player   on ipl_player_reservations (offer_id, player_id);
create index if not exists idx_ipl_roster_player on ipl_team_rosters (tournament_id, player_id);

-- pool size: a manager only needs 10 owned + 11 fresh cards = 21 distinct cricketers (+ margin)
create or replace function ipl_required_pool(p_humans int, p_margin int default 20) returns int
language sql immutable as $$ select 21 + p_margin $$;

create or replace function ipl_team_json(t ipl_tournament_teams) returns jsonb
language sql stable as $$
  select jsonb_build_object('id', t.id, 'tournament_id', t.tournament_id, 'team_no', t.team_no, 'kind', t.kind,
           'user_id', t.user_id, 'franchise_code', t.franchise_code, 'name', t.name,
           'short_name', t.short_name, 'owner_name', t.owner_name, 'squad_complete', t.squad_complete,
           'auto_draft', t.auto_draft, 'timeouts', t.timeouts,
           'batting_order_confirmed', coalesce(t.batting_order_confirmed, false),
           'batting_order_deadline_at', t.batting_order_deadline_at,
           'batting_order_message_id', t.batting_order_message_id)
$$;

create or replace function ipl_team_by_id(p_team_id bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select ipl_team_json(tm) from ipl_tournament_teams tm where tm.id = p_team_id;
$$;

-- offers: only the manager's OWN squad is excluded (players on other teams stay draftable). No cross-team reservations.
create or replace function ipl_open_offer(p_team_id bigint, p_force_roles jsonb default null)
returns jsonb language plpgsql security definer set search_path = public, ipl_private as $$
declare tm ipl_tournament_teams%rowtype; t ipl_tournaments%rowtype; o ipl_draft_offers%rowtype;
        n int; ids uuid[]; extra uuid; dl timestamptz; oid bigint;
begin
  select * into tm from ipl_tournament_teams where id = p_team_id;
  if not found then return jsonb_build_object('status', 'NO_TEAM'); end if;
  select * into t from ipl_tournaments where id = tm.tournament_id for update;
  if tm.kind = 'HUMAN' and t.state <> 'DRAFTING' then return jsonb_build_object('status', 'CLOSED'); end if;
  if tm.kind = 'SYSTEM' and t.state not in ('DRAFTING', 'SYSTEM_TEAM_GENERATION') then return jsonb_build_object('status', 'CLOSED'); end if;

  select * into o from ipl_draft_offers where team_id = p_team_id and status = 'OPEN';
  if found then
    return ipl_offer_json(o.id) || jsonb_build_object('status', 'OPEN', 'picks_done', o.round_no - 1);
  end if;

  select count(*) into n from ipl_draft_picks where team_id = p_team_id;
  if n >= 11 then return jsonb_build_object('status', 'DONE', 'picks_done', n); end if;

  select array_agg(id) into ids from (
    select pl.player_id as id
      from ipl_tournament_pool pl
     where pl.tournament_id = t.id
       and not exists (select 1 from ipl_team_rosters r where r.team_id = p_team_id and r.player_id = pl.player_id)
     order by random() limit 11) s;
  if ids is null or cardinality(ids) < 11 then
    return jsonb_build_object('status', 'POOL_EXHAUSTED', 'available', coalesce(cardinality(ids), 0));
  end if;

  if p_force_roles is not null and tm.kind = 'SYSTEM' and not exists (
       select 1 from ipl_players p where p.id = any(ids) and p.role in (select jsonb_array_elements_text(p_force_roles))) then
    select pl.player_id into extra
      from ipl_tournament_pool pl join ipl_players p on p.id = pl.player_id
     where pl.tournament_id = t.id and p.role in (select jsonb_array_elements_text(p_force_roles))
       and not exists (select 1 from ipl_team_rosters r where r.team_id = p_team_id and r.player_id = pl.player_id)
     order by random() limit 1;
    if extra is not null then ids[11] := extra; end if;
  end if;

  dl := now() + make_interval(secs => t.draft_seconds);
  insert into ipl_draft_offers (tournament_id, team_id, round_no, deadline_at)
  values (t.id, p_team_id, n + 1, dl) returning id into oid;
  insert into ipl_draft_offer_candidates (offer_id, position, player_id)
    select oid, row_number() over (order by random()), x from unnest(ids) x;
  return ipl_offer_json(oid) || jsonb_build_object('status', 'OPEN', 'picks_done', n);
end $$;

create or replace function ipl_batting_order_state(p_team_id bigint) returns jsonb
language plpgsql stable security definer set search_path = public, ipl_private as $$
declare v_players jsonb; v_ordered jsonb; v_remaining jsonb;
begin
  select coalesce(jsonb_agg(jsonb_build_object('id', p.id, 'name', p.display_name, 'role', p.role, 'slot', r.slot,
                                               'batting_order', r.batting_order) order by r.slot), '[]'::jsonb)
    into v_players from ipl_team_rosters r join ipl_players p on p.id = r.player_id where r.team_id = p_team_id;
  select coalesce(jsonb_agg(jsonb_build_object('id', p.id, 'name', p.display_name, 'role', p.role, 'slot', r.slot,
                                               'batting_order', r.batting_order) order by r.batting_order), '[]'::jsonb)
    into v_ordered from ipl_team_rosters r join ipl_players p on p.id = r.player_id
   where r.team_id = p_team_id and r.batting_order is not null;
  select coalesce(jsonb_agg(jsonb_build_object('pos', r.slot,
           'player', jsonb_build_object('id', p.id, 'name', p.display_name, 'role', p.role, 'slot', r.slot)) order by r.slot), '[]'::jsonb)
    into v_remaining from ipl_team_rosters r join ipl_players p on p.id = r.player_id
   where r.team_id = p_team_id and r.batting_order is null;
  return jsonb_build_object('players', v_players, 'ordered', v_ordered, 'remaining', v_remaining);
end $$;

create or replace function ipl_batting_order_begin(p_team_id bigint, p_seconds int) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare tm ipl_tournament_teams%rowtype;
begin
  select * into tm from ipl_tournament_teams where id = p_team_id for update;
  if not found then return jsonb_build_object('status', 'NO_TEAM'); end if;
  if tm.batting_order_deadline_at is null and not tm.batting_order_confirmed then
    update ipl_tournament_teams set batting_order_deadline_at = now() + make_interval(secs => p_seconds)
     where id = p_team_id returning * into tm;
  end if;
  return jsonb_build_object('status', 'OK', 'deadline_at', tm.batting_order_deadline_at,
                            'confirmed', tm.batting_order_confirmed, 'prompted', tm.batting_order_prompted_at is not null);
end $$;

create or replace function ipl_batting_order_mark_prompted(p_team_id bigint, p_message_id bigint) returns void
language sql security definer set search_path = public, ipl_private as $$
  update ipl_tournament_teams set batting_order_prompted_at = now(), batting_order_message_id = p_message_id where id = p_team_id
$$;

create or replace function ipl_batting_order_pick(p_team_id bigint, p_pos smallint) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare v_curr smallint; v_next smallint; v_conf boolean;
begin
  select batting_order_confirmed into v_conf from ipl_tournament_teams where id = p_team_id for update;
  if not found then return jsonb_build_object('status', 'NO_TEAM'); end if;
  if v_conf then return jsonb_build_object('status', 'DONE'); end if;
  select batting_order into v_curr from ipl_team_rosters where team_id = p_team_id and slot = p_pos;
  if not found then return jsonb_build_object('status', 'NOT_FOUND'); end if;
  if v_curr is not null then return jsonb_build_object('status', 'ALREADY'); end if;
  select coalesce(max(batting_order), 0) + 1 into v_next from ipl_team_rosters where team_id = p_team_id;
  if v_next > 11 then return jsonb_build_object('status', 'FULL'); end if;
  update ipl_team_rosters set batting_order = v_next where team_id = p_team_id and slot = p_pos;
  return jsonb_build_object('status', case when v_next = 11 then 'LAST' else 'OK' end, 'order', v_next);
end $$;

create or replace function ipl_batting_order_undo(p_team_id bigint) returns void
language plpgsql security definer set search_path = public, ipl_private as $$
declare v_max smallint;
begin
  perform 1 from ipl_tournament_teams where id = p_team_id and not batting_order_confirmed for update;
  if not found then return; end if;
  select max(batting_order) into v_max from ipl_team_rosters where team_id = p_team_id;
  if v_max is not null then update ipl_team_rosters set batting_order = null where team_id = p_team_id and batting_order = v_max; end if;
end $$;

create or replace function ipl_batting_order_reset(p_team_id bigint) returns void
language plpgsql security definer set search_path = public, ipl_private as $$
begin
  perform 1 from ipl_tournament_teams where id = p_team_id and not batting_order_confirmed for update;
  if not found then return; end if;
  update ipl_team_rosters set batting_order = null where team_id = p_team_id;
end $$;

create or replace function ipl_batting_order_confirm(p_team_id bigint) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare v_cnt int; v_distinct int;
begin
  perform 1 from ipl_tournament_teams where id = p_team_id for update;
  if not found then return jsonb_build_object('status', 'NO_TEAM'); end if;
  select count(*), count(distinct batting_order) into v_cnt, v_distinct
    from ipl_team_rosters where team_id = p_team_id and batting_order is not null;
  if v_cnt < 11 or v_distinct <> 11 then return jsonb_build_object('status', 'INCOMPLETE', 'count', v_cnt); end if;
  update ipl_tournament_teams set batting_order_confirmed = true where id = p_team_id;
  return jsonb_build_object('status', 'OK');
end $$;

-- keeps whatever the manager already picked, fills the rest with the engine's own rule, then confirms
create or replace function ipl_batting_order_autofill(p_team_id bigint) returns void
language plpgsql security definer set search_path = public, ipl_private as $$
declare v_base smallint;
begin
  perform 1 from ipl_tournament_teams where id = p_team_id for update;
  select coalesce(max(batting_order), 0) into v_base from ipl_team_rosters where team_id = p_team_id;
  with ranked as (
    select r.id, row_number() over (
             order by (coalesce(tr.batting_rating, 50) * 0.7 + coalesce(tr.power_hitting, 50) * 0.3
                       + case when p.role in ('BAT', 'WK') then 3 else 0 end) desc, r.slot) as rn
      from ipl_team_rosters r
      join ipl_players p on p.id = r.player_id
      left join ipl_private.ipl_tournament_ratings tr on tr.tournament_id = r.tournament_id and tr.player_id = r.player_id
     where r.team_id = p_team_id and r.batting_order is null)
  update ipl_team_rosters ro set batting_order = v_base + rk.rn from ranked rk where ro.id = rk.id;
  update ipl_tournament_teams set batting_order_confirmed = true where id = p_team_id;
end $$;

create or replace function ipl_batting_order_auto_assign(p_team_id bigint) returns void
language plpgsql security definer set search_path = public, ipl_private as $$
begin
  if exists (select 1 from ipl_tournament_teams where id = p_team_id and batting_order_confirmed) then return; end if;
  update ipl_team_rosters set batting_order = null where team_id = p_team_id;
  perform ipl_batting_order_autofill(p_team_id);
end $$;

create or replace function ipl_batting_order_set_order(p_team_id bigint, p_player_ids text[]) returns void
language plpgsql security definer set search_path = public, ipl_private as $$
begin
  if coalesce(array_length(p_player_ids, 1), 0) <> 11
     or (select count(distinct x) from unnest(p_player_ids) x) <> 11
     or (select count(*) from ipl_team_rosters r where r.team_id = p_team_id and r.player_id::text = any (p_player_ids)) <> 11 then
    raise exception 'batting order must list the 11 distinct players of the squad' using errcode = 'P0001';
  end if;
  update ipl_team_rosters r set batting_order = o.n
    from unnest(p_player_ids) with ordinality as o(pid, n)
   where r.team_id = p_team_id and r.player_id::text = o.pid;
  update ipl_tournament_teams set batting_order_confirmed = true where id = p_team_id;
end $$;

create or replace function ipl_teams_pending_batting_order(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(ipl_team_json(tm)), '[]'::jsonb)
    from ipl_tournament_teams tm
   where tm.tournament_id = p_tid and tm.kind = 'HUMAN' and tm.squad_complete and not coalesce(tm.batting_order_confirmed, false)
$$;

create or replace function ipl_batting_order_due(p_limit int default 50) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(ipl_team_json(tm) || jsonb_build_object(
           'expired', tm.batting_order_deadline_at is not null and tm.batting_order_deadline_at < now(),
           'needs_clock', tm.batting_order_deadline_at is null,
           'needs_prompt', tm.batting_order_prompted_at is null)), '[]'::jsonb)
    from (select * from ipl_tournament_teams x
           where x.kind = 'HUMAN' and x.squad_complete and not x.batting_order_confirmed
             and exists (select 1 from ipl_tournaments t where t.id = x.tournament_id and t.state = 'DRAFTING')
           order by x.id limit p_limit) tm
$$;

create or replace function ipl_finish_drafting(p_tid bigint, p_expected_system int default 4) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype; h_open int; bo_open int; sys_n int; sys_open int;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found then return jsonb_build_object('status', 'NOT_FOUND'); end if;
  if t.state in ('SYSTEM_TEAM_GENERATION', 'FIXTURE_GENERATION', 'LEAGUE_RUNNING', 'LEAGUE_COMPLETED', 'PLAYOFF_ELIMINATOR',
                 'PLAYOFF_QUALIFIER_1', 'PLAYOFF_QUALIFIER_2', 'PLAYOFF_FINAL', 'COMPLETED') then
    return jsonb_build_object('status', 'ALREADY', 'state', t.state);
  end if;
  if t.state <> 'DRAFTING' then return jsonb_build_object('status', 'BAD_STATE', 'state', t.state); end if;
  select count(*) filter (where not squad_complete), count(*) filter (where not batting_order_confirmed)
    into h_open, bo_open from ipl_tournament_teams where tournament_id = p_tid and kind = 'HUMAN';
  if h_open > 0 then return jsonb_build_object('status', 'NOT_READY', 'why', 'humans_drafting', 'teams', h_open); end if;
  if bo_open > 0 then return jsonb_build_object('status', 'NOT_READY', 'why', 'batting_order', 'teams', bo_open); end if;
  select count(*), count(*) filter (where not squad_complete) into sys_n, sys_open
    from ipl_tournament_teams where tournament_id = p_tid and kind = 'SYSTEM';
  if sys_n < p_expected_system or sys_open > 0 then
    return jsonb_build_object('status', 'NOT_READY', 'why', 'system_teams', 'have', sys_n, 'incomplete', sys_open);
  end if;
  update ipl_tournaments set state = 'SYSTEM_TEAM_GENERATION' where id = p_tid;
  return jsonb_build_object('status', 'OK');
end $$;

create or replace function ipl_begin_system_teams(p_tid bigint, p_franchises jsonb) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype; h int; f jsonb; i int := 0;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found then return jsonb_build_object('status', 'NOT_FOUND'); end if;
  if t.state not in ('DRAFTING', 'SYSTEM_TEAM_GENERATION') then return jsonb_build_object('status', 'BAD_STATE', 'state', t.state); end if;
  select count(*) into h from ipl_tournament_teams where tournament_id = p_tid and kind = 'HUMAN';
  for f in select * from jsonb_array_elements(p_franchises) loop
    i := i + 1;
    insert into ipl_tournament_teams (tournament_id, team_no, kind, franchise_code, name, short_name)
    values (p_tid, h + i, 'SYSTEM', f->>'code', f->>'name', f->>'code')
    on conflict do nothing;
  end loop;
  return jsonb_build_object('status', 'OK', 'teams', i);
end $$;

create or replace function ipl_engine_squads(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(jsonb_build_object(
      'team_id', tm.id, 'team_no', tm.team_no, 'name', tm.name, 'short_name', tm.short_name, 'kind', tm.kind,
      'batting_order_confirmed', coalesce(tm.batting_order_confirmed, false),
      'players', (select coalesce(jsonb_agg(jsonb_build_object(
                    'id', p.id, 'name', p.display_name, 'role', p.role, 'bowling_type', p.bowling_type,
                    'batting_style', p.batting_style, 'slot', r.slot,
                    'batting_order', r.batting_order,
                    'ratings', to_jsonb(tr) - 'tournament_id' - 'player_id') order by r.batting_order nulls last, r.slot), '[]'::jsonb)
                    from ipl_team_rosters r
                    join ipl_players p on p.id = r.player_id
                    left join ipl_private.ipl_tournament_ratings tr
                           on tr.tournament_id = r.tournament_id and tr.player_id = r.player_id
                   where r.team_id = tm.id)) order by tm.team_no), '[]'::jsonb)
    from ipl_tournament_teams tm where tm.tournament_id = p_tid
$$;

-- settle: match awards carry the team
create or replace function ipl_settle_match(p_fixture_id bigint, p_result jsonb) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare f ipl_fixtures%rowtype; t ipl_tournaments%rowtype; mid bigint; inn jsonb; iid bigint;
        w bigint; loser bigint; t1 bigint; t2 bigint; rt text; bat_runs int; bat_out int; ex_runs int;
        bowl_balls int; stage_next text; fx bigint; rk jsonb; elim_w bigint; q1_l bigint; q1_w bigint; q2_w bigint;
        nr jsonb; side text; tid bigint; opp bigint;
begin
  select * into f from ipl_fixtures where id = p_fixture_id for update;
  if not found then return jsonb_build_object('status', 'NOT_FOUND'); end if;
  if f.status = 'COMPLETED' then return jsonb_build_object('status', 'ALREADY'); end if;
  select * into t from ipl_tournaments where id = f.tournament_id for update;
  if t.state in ('CANCELLED','COMPLETED','FAILED_RECOVERABLE') then return jsonb_build_object('status', 'CLOSED'); end if;

  t1 := (p_result->>'team1_id')::bigint; t2 := (p_result->>'team2_id')::bigint;
  if not ((t1 = f.home_team_id and t2 = f.away_team_id) or (t1 = f.away_team_id and t2 = f.home_team_id)) then
    raise exception 'result teams do not match fixture %', p_fixture_id;
  end if;
  w := nullif(p_result->>'winner_team_id', '')::bigint;
  rt := p_result->>'result_type';
  if f.stage <> 'LEAGUE' and w is null then raise exception 'a playoff match needs a winner'; end if;
  if f.stage = 'LEAGUE' and w is null and rt <> 'NO_RESULT' then raise exception 'league match needs a winner'; end if;

  -- scorecard consistency checks (an engine bug must never be stored as a result)
  for inn in select * from jsonb_array_elements(p_result->'innings') loop
    select coalesce(sum((b->>'runs')::int), 0), coalesce(sum(case when (b->>'out')::boolean then 1 else 0 end), 0)
      into bat_runs, bat_out from jsonb_array_elements(inn->'batting') b;
    select coalesce(sum((x.value)::int), 0) into ex_runs from jsonb_each_text(inn->'extras') x;
    if bat_runs + ex_runs <> (inn->>'runs')::int then
      raise exception 'innings % runs % != batters % + extras %', inn->>'innings_no', inn->>'runs', bat_runs, ex_runs;
    end if;
    if bat_out <> (inn->>'wickets')::int then raise exception 'innings % wickets mismatch', inn->>'innings_no'; end if;
    select coalesce(sum((b->>'legal_balls')::int), 0) into bowl_balls from jsonb_array_elements(inn->'bowling') b;
    if bowl_balls <> (inn->>'legal_balls')::int then raise exception 'innings % balls mismatch', inn->>'innings_no'; end if;
    if exists (select 1 from jsonb_array_elements(inn->'bowling') b where (b->>'legal_balls')::int > 24
                and not coalesce((inn->>'is_super_over')::boolean, false)) then
      raise exception 'a bowler exceeded 4 overs';
    end if;
  end loop;

  -- the same cricketer can wear both shirts in one match (cross-team drafting): awards must name the TEAM too
  if nullif(p_result->>'pom_player_id','') is not null
     and nullif(p_result->>'pom_team_id','')::bigint is distinct from t1
     and nullif(p_result->>'pom_team_id','')::bigint is distinct from t2 then
    raise exception 'player of the match needs a team that played the match';
  end if;

  insert into ipl_matches (fixture_id, tournament_id, stage, match_no, team1_id, team2_id, toss_winner_id, toss_decision,
                           winner_team_id, result_type, margin, super_overs, pom_player_id, best_bowler_id, pom_team_id, best_bowler_team_id, summary, rng_seed)
  values (f.id, f.tournament_id, f.stage, f.match_no, t1, t2, nullif(p_result->>'toss_winner_id','')::bigint,
          nullif(p_result->>'toss_decision',''), w, rt, nullif(p_result->>'margin','')::int,
          coalesce((p_result->>'super_overs')::smallint, 0), nullif(p_result->>'pom_player_id','')::uuid,
          nullif(p_result->>'best_bowler_id','')::uuid,
          nullif(p_result->>'pom_team_id','')::bigint, nullif(p_result->>'best_bowler_team_id','')::bigint,
          p_result->>'summary', p_result->>'seed')
  returning id into mid;

  for inn in select * from jsonb_array_elements(p_result->'innings') loop
    insert into ipl_innings (match_id, innings_no, is_super_over, batting_team_id, bowling_team_id, runs, wickets,
                             legal_balls, extras, fall_of_wickets, target, all_out)
    values (mid, (inn->>'innings_no')::smallint, coalesce((inn->>'is_super_over')::boolean, false),
            (inn->>'batting_team_id')::bigint, (inn->>'bowling_team_id')::bigint, (inn->>'runs')::int,
            (inn->>'wickets')::smallint, (inn->>'legal_balls')::int, inn->'extras', coalesce(inn->'fall_of_wickets', '[]'::jsonb),
            nullif(inn->>'target','')::int, coalesce((inn->>'all_out')::boolean, false))
    returning id into iid;
    insert into ipl_batting_scorecards (innings_id, player_id, batting_pos, did_bat, runs, balls, fours, sixes, is_out, dismissal, bowler_id, fielder_id)
      select iid, (b->>'player_id')::uuid, (b->>'pos')::smallint, (b->>'did_bat')::boolean, (b->>'runs')::int,
             (b->>'balls')::int, (b->>'fours')::int, (b->>'sixes')::int, (b->>'out')::boolean,
             b->>'dismissal', nullif(b->>'bowler_id','')::uuid, nullif(b->>'fielder_id','')::uuid
        from jsonb_array_elements(inn->'batting') b;
    insert into ipl_bowling_scorecards (innings_id, player_id, legal_balls, runs, wickets, maidens, wides, noballs, dots)
      select iid, (b->>'player_id')::uuid, (b->>'legal_balls')::int, (b->>'runs')::int, (b->>'wickets')::int,
             (b->>'maidens')::int, (b->>'wides')::int, (b->>'noballs')::int, (b->>'dots')::int
        from jsonb_array_elements(inn->'bowling') b;
    if inn ? 'balls' then
      insert into ipl_ball_events (innings_id, seq, over_no, ball_no, striker_id, non_striker_id, bowler_id,
                                   runs_bat, extras_type, extras_runs, wicket_kind, player_out_id, fielder_id, legal)
        select iid, (e.v->>0)::int, (e.v->>1)::smallint, (e.v->>2)::smallint, nullif(e.v->>3,'')::uuid,
               nullif(e.v->>4,'')::uuid, nullif(e.v->>5,'')::uuid, (e.v->>6)::smallint, nullif(e.v->>7,''),
               (e.v->>8)::smallint, nullif(e.v->>9,''), nullif(e.v->>10,'')::uuid, nullif(e.v->>11,'')::uuid,
               (e.v->>12)::boolean
          from jsonb_array_elements(inn->'balls') as e(v);
    end if;
  end loop;

  update ipl_fixtures set status = 'COMPLETED', completed_at = now() where id = f.id;

  if f.stage = 'LEAGUE' then
    nr := p_result->'nrr';
    foreach side in array array['team1','team2'] loop
      tid := case when side = 'team1' then t1 else t2 end;
      update ipl_standings s set
        played = played + 1,
        won = won + case when w = tid then 1 else 0 end,
        lost = lost + case when w is not null and w <> tid then 1 else 0 end,
        no_result = no_result + case when w is null then 1 else 0 end,
        points = points + case when w = tid then 2 when w is null then 1 else 0 end,
        runs_for = runs_for + (nr->side->>'runs_for')::int,
        balls_faced = balls_faced + (nr->side->>'balls_faced')::int,
        runs_against = runs_against + (nr->side->>'runs_against')::int,
        balls_bowled = balls_bowled + (nr->side->>'balls_bowled')::int,
        updated_at = now()
      where s.tournament_id = f.tournament_id and s.team_id = tid;
    end loop;
  else
    loser := case when w = t1 then t2 else t1 end;
    if f.stage = 'ELIMINATOR' then
      update ipl_tournaments set state = 'PLAYOFF_QUALIFIER_1' where id = f.tournament_id;
    elsif f.stage = 'QUALIFIER_1' then
      select (select m.winner_team_id from ipl_matches m join ipl_fixtures x on x.id = m.fixture_id
               where x.tournament_id = f.tournament_id and x.stage = 'ELIMINATOR') into elim_w;
      insert into ipl_fixtures (tournament_id, stage, match_no, home_team_id, away_team_id)
        values (f.tournament_id, 'QUALIFIER_2', f.match_no + 1, loser, elim_w) returning id into fx;
      update ipl_playoff_matches set fixture_id = fx where tournament_id = f.tournament_id and stage = 'QUALIFIER_2';
      update ipl_tournaments set state = 'PLAYOFF_QUALIFIER_2' where id = f.tournament_id;
    elsif f.stage = 'QUALIFIER_2' then
      select m.winner_team_id into q1_w from ipl_matches m join ipl_fixtures x on x.id = m.fixture_id
       where x.tournament_id = f.tournament_id and x.stage = 'QUALIFIER_1';
      insert into ipl_fixtures (tournament_id, stage, match_no, home_team_id, away_team_id)
        values (f.tournament_id, 'FINAL', f.match_no + 1, q1_w, w) returning id into fx;
      update ipl_playoff_matches set fixture_id = fx where tournament_id = f.tournament_id and stage = 'FINAL';
      update ipl_tournaments set state = 'PLAYOFF_FINAL' where id = f.tournament_id;
    elsif f.stage = 'FINAL' then
      update ipl_tournaments set state = 'COMPLETED', champion_team_id = w, runner_up_team_id = loser,
             completed_at = now() where id = f.tournament_id;
    end if;
  end if;
  return jsonb_build_object('status', 'OK', 'match_id', mid, 'stage', f.stage, 'tournament_id', f.tournament_id);
end $$;

create or replace function ipl_match_card(p_match_id bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select jsonb_build_object(
    'match_id', m.id, 'tournament_id', m.tournament_id, 'stage', m.stage, 'match_no', m.match_no,
    'team1', ipl_team_json(t1), 'team2', ipl_team_json(t2), 'winner_team_id', m.winner_team_id,
    'result_type', m.result_type, 'margin', m.margin, 'super_overs', m.super_overs, 'summary', m.summary,
    'toss_winner_id', m.toss_winner_id, 'toss_decision', m.toss_decision,
    'innings', coalesce((select jsonb_agg(jsonb_build_object('innings_no', i.innings_no, 'runs', i.runs,
                  'wickets', i.wickets, 'legal_balls', i.legal_balls, 'batting_team_id', i.batting_team_id,
                  'is_super_over', i.is_super_over, 'target', i.target) order by i.innings_no)
                  from ipl_innings i where i.match_id = m.id), '[]'::jsonb),
    'pom', (select jsonb_build_object('player_id', p.id, 'name', p.display_name,
              'runs', coalesce((select sum(b.runs) from ipl_batting_scorecards b join ipl_innings i on i.id = b.innings_id
                                 where i.match_id = m.id and not i.is_super_over and i.batting_team_id = x.pom_team and b.player_id = p.id), 0),
              'balls', coalesce((select sum(b.balls) from ipl_batting_scorecards b join ipl_innings i on i.id = b.innings_id
                                  where i.match_id = m.id and not i.is_super_over and i.batting_team_id = x.pom_team and b.player_id = p.id), 0),
              'wickets', coalesce((select sum(w.wickets) from ipl_bowling_scorecards w join ipl_innings i on i.id = w.innings_id
                                    where i.match_id = m.id and not i.is_super_over and i.bowling_team_id = x.pom_team and w.player_id = p.id), 0),
              'bowl_runs', coalesce((select sum(w.runs) from ipl_bowling_scorecards w join ipl_innings i on i.id = w.innings_id
                                      where i.match_id = m.id and not i.is_super_over and i.bowling_team_id = x.pom_team and w.player_id = p.id), 0),
              'team_id', x.pom_team)
             from ipl_players p where p.id = m.pom_player_id),
    'best_bowler', (select jsonb_build_object('player_id', p.id, 'name', p.display_name, 'team_id', x.bb_team,
              'wickets', coalesce((select sum(w.wickets) from ipl_bowling_scorecards w join ipl_innings i on i.id = w.innings_id
                                    where i.match_id = m.id and not i.is_super_over and i.bowling_team_id = x.bb_team and w.player_id = p.id), 0),
              'runs', coalesce((select sum(w.runs) from ipl_bowling_scorecards w join ipl_innings i on i.id = w.innings_id
                                 where i.match_id = m.id and not i.is_super_over and i.bowling_team_id = x.bb_team and w.player_id = p.id), 0))
             from ipl_players p where p.id = m.best_bowler_id))
    from ipl_matches m
    join ipl_tournament_teams t1 on t1.id = m.team1_id
    join ipl_tournament_teams t2 on t2.id = m.team2_id
    cross join lateral (select
        coalesce(m.pom_team_id, (select r.team_id from ipl_team_rosters r where r.tournament_id = m.tournament_id
                                  and r.player_id = m.pom_player_id and r.team_id in (m.team1_id, m.team2_id) limit 1)) as pom_team,
        coalesce(m.best_bowler_team_id, (select r.team_id from ipl_team_rosters r where r.tournament_id = m.tournament_id
                                  and r.player_id = m.best_bowler_id and r.team_id in (m.team1_id, m.team2_id) limit 1)) as bb_team) x
   where m.id = p_match_id
$$;

create or replace function ipl_player_tournament_stats(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  with inn as (
    select i.id, i.match_id, i.batting_team_id, i.bowling_team_id
      from ipl_innings i join ipl_matches m on m.id = i.match_id
     where m.tournament_id = p_tid and not i.is_super_over),
  bat as (
    select b.player_id, inn.batting_team_id as team_id, count(*) filter (where b.did_bat) as innings, sum(b.runs) runs,
           sum(b.balls) balls, sum(b.fours) fours, sum(b.sixes) sixes, count(*) filter (where b.is_out) outs,
           max(b.runs) high, (array_agg(b.balls order by b.runs desc, b.balls asc))[1] as high_balls
      from ipl_batting_scorecards b join inn on inn.id = b.innings_id where b.did_bat group by b.player_id, inn.batting_team_id),
  bowl as (
    select w.player_id, inn.bowling_team_id as team_id, count(*) filter (where w.legal_balls > 0) as innings,
           sum(w.legal_balls) balls, sum(w.runs) runs, sum(w.wickets) wickets, sum(w.maidens) maidens, max(w.wickets) best_w
      from ipl_bowling_scorecards w join inn on inn.id = w.innings_id group by w.player_id, inn.bowling_team_id),
  fld as (
    select b.fielder_id as player_id, inn.bowling_team_id as team_id,
           count(*) filter (where b.dismissal = 'caught') catches,
           count(*) filter (where b.dismissal = 'stumped') stumpings,
           count(*) filter (where b.dismissal = 'run out') run_outs
      from ipl_batting_scorecards b join inn on inn.id = b.innings_id
     where b.fielder_id is not null group by b.fielder_id, inn.bowling_team_id),
  pom as (
    select m.pom_player_id as player_id,
           coalesce(m.pom_team_id, (select r.team_id from ipl_team_rosters r where r.tournament_id = m.tournament_id
                                     and r.player_id = m.pom_player_id and r.team_id in (m.team1_id, m.team2_id) limit 1)) as team_id,
           count(*) n
      from ipl_matches m where m.tournament_id = p_tid and m.pom_player_id is not null group by 1, 2),
  everyone as (
    select player_id, team_id from bat union select player_id, team_id from bowl
    union select player_id, team_id from fld union select player_id, team_id from pom)
  select coalesce(jsonb_agg(jsonb_build_object(
      'player_id', e.player_id, 'name', p.display_name, 'team_id', e.team_id,
      'bat_innings', coalesce(bat.innings, 0), 'runs', coalesce(bat.runs, 0), 'balls', coalesce(bat.balls, 0),
      'fours', coalesce(bat.fours, 0), 'sixes', coalesce(bat.sixes, 0), 'outs', coalesce(bat.outs, 0),
      'high', coalesce(bat.high, 0), 'high_balls', coalesce(bat.high_balls, 0),
      'bowl_balls', coalesce(bowl.balls, 0), 'bowl_runs', coalesce(bowl.runs, 0), 'wickets', coalesce(bowl.wickets, 0),
      'maidens', coalesce(bowl.maidens, 0), 'best_w', coalesce(bowl.best_w, 0),
      'catches', coalesce(fld.catches, 0), 'stumpings', coalesce(fld.stumpings, 0), 'run_outs', coalesce(fld.run_outs, 0),
      'pom', coalesce(pom.n, 0))), '[]'::jsonb)
    from everyone e
    join ipl_players p on p.id = e.player_id
    left join bat  on bat.player_id  = e.player_id and bat.team_id  = e.team_id
    left join bowl on bowl.player_id = e.player_id and bowl.team_id = e.team_id
    left join fld  on fld.player_id  = e.player_id and fld.team_id  = e.team_id
    left join pom  on pom.player_id  = e.player_id and pom.team_id  = e.team_id
$$;

-- state machine: restore the strict transitions (migration_ipl_v2.sql had loosened them to paper over the stuck-DRAFTING loop;
-- DRAFTING may now only leave through ipl_finish_drafting -> SYSTEM_TEAM_GENERATION -> FIXTURE_GENERATION)
create or replace function ipl_valid_transition(p_old text, p_new text) returns boolean
language sql immutable as $$
  select case
    when p_old = p_new then true
    when p_old in ('COMPLETED','CANCELLED') then false
    when p_new = 'CANCELLED' then true
    when p_new = 'FAILED_RECOVERABLE' then p_old <> 'LOBBY'
    when p_old = 'FAILED_RECOVERABLE' then p_new in ('DRAFTING','SYSTEM_TEAM_GENERATION','FIXTURE_GENERATION',
         'LEAGUE_RUNNING','LEAGUE_COMPLETED','PLAYOFF_ELIMINATOR','PLAYOFF_QUALIFIER_1',
         'PLAYOFF_QUALIFIER_2','PLAYOFF_FINAL')
    when p_old = 'LOBBY'                  then p_new = 'DRAFTING'
    when p_old = 'DRAFTING'               then p_new = 'SYSTEM_TEAM_GENERATION'
    when p_old = 'SYSTEM_TEAM_GENERATION' then p_new = 'FIXTURE_GENERATION'
    when p_old = 'FIXTURE_GENERATION'     then p_new = 'LEAGUE_RUNNING'
    when p_old = 'LEAGUE_RUNNING'         then p_new = 'LEAGUE_COMPLETED'
    when p_old = 'LEAGUE_COMPLETED'       then p_new = 'PLAYOFF_ELIMINATOR'
    when p_old = 'PLAYOFF_ELIMINATOR'     then p_new = 'PLAYOFF_QUALIFIER_1'
    when p_old = 'PLAYOFF_QUALIFIER_1'    then p_new = 'PLAYOFF_QUALIFIER_2'
    when p_old = 'PLAYOFF_QUALIFIER_2'    then p_new = 'PLAYOFF_FINAL'
    when p_old = 'PLAYOFF_FINAL'          then p_new = 'COMPLETED'
    else false end
$$;

-- ══════════════════════════════════════════════════════════════════════
-- V4 PATCH (appended to v3; safe to re-run)
--   • Reduce system franchises from 5 → 4 (drop DC / Delhi Capitals)
--   • ipl_finish_drafting: default p_expected_system changed 5 → 4
--   • ipl_create_fixtures: allow 6–16 teams (was 7–13) so 4 system + 2 human works
-- ══════════════════════════════════════════════════════════════════════

create or replace function ipl_finish_drafting(p_tid bigint, p_expected_system int default 4) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype; h_open int; bo_open int; sys_n int; sys_open int;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found then return jsonb_build_object('status', 'NOT_FOUND'); end if;
  if t.state in ('SYSTEM_TEAM_GENERATION', 'FIXTURE_GENERATION', 'LEAGUE_RUNNING', 'LEAGUE_COMPLETED', 'PLAYOFF_ELIMINATOR',
                 'PLAYOFF_QUALIFIER_1', 'PLAYOFF_QUALIFIER_2', 'PLAYOFF_FINAL', 'COMPLETED') then
    return jsonb_build_object('status', 'ALREADY', 'state', t.state);
  end if;
  if t.state <> 'DRAFTING' then return jsonb_build_object('status', 'BAD_STATE', 'state', t.state); end if;
  select count(*) filter (where not squad_complete), count(*) filter (where not batting_order_confirmed)
    into h_open, bo_open from ipl_tournament_teams where tournament_id = p_tid and kind = 'HUMAN';
  if h_open > 0 then return jsonb_build_object('status', 'NOT_READY', 'why', 'humans_drafting', 'teams', h_open); end if;
  if bo_open > 0 then return jsonb_build_object('status', 'NOT_READY', 'why', 'batting_order', 'teams', bo_open); end if;
  select count(*), count(*) filter (where not squad_complete) into sys_n, sys_open
    from ipl_tournament_teams where tournament_id = p_tid and kind = 'SYSTEM';
  if sys_n < p_expected_system or sys_open > 0 then
    return jsonb_build_object('status', 'NOT_READY', 'why', 'system_teams', 'have', sys_n, 'incomplete', sys_open);
  end if;
  update ipl_tournaments set state = 'SYSTEM_TEAM_GENERATION' where id = p_tid;
  return jsonb_build_object('status', 'OK');
end $$;

create or replace function ipl_create_fixtures(p_tid bigint, p_fixtures jsonb) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype; n int; expected int; cnt int; f jsonb;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found then return jsonb_build_object('status', 'NOT_FOUND'); end if;
  if t.state = 'LEAGUE_RUNNING' or exists (select 1 from ipl_fixtures where tournament_id = p_tid) then
    return jsonb_build_object('status', 'OK', 'already', true);
  end if;
  if t.state <> 'FIXTURE_GENERATION' then return jsonb_build_object('status', 'BAD_STATE', 'state', t.state); end if;
  select count(*) into n from ipl_tournament_teams where tournament_id = p_tid;
  -- allow 6–16 teams (4 system + 2..8 human)
  if n < 6 or n > 16 then return jsonb_build_object('status', 'BAD_TEAM_COUNT', 'teams', n); end if;
  expected := n * (n - 1);
  cnt := jsonb_array_length(p_fixtures);
  if cnt <> expected then return jsonb_build_object('status', 'BAD_FIXTURE_COUNT', 'have', cnt, 'need', expected); end if;
  for f in select * from jsonb_array_elements(p_fixtures) loop
    insert into ipl_fixtures (tournament_id, stage, match_no, matchday, leg, home_team_id, away_team_id)
    values (p_tid, 'LEAGUE', (f->>'match_no')::int, (f->>'matchday')::int, (f->>'leg')::smallint,
            (f->>'home')::bigint, (f->>'away')::bigint);
  end loop;
  -- every ordered pair exactly once => every pair of teams meets exactly twice
  if (select count(distinct (least(home_team_id, away_team_id), greatest(home_team_id, away_team_id)))
        from ipl_fixtures where tournament_id = p_tid) <> n * (n - 1) / 2 then
    raise exception 'fixture list does not cover every pair';
  end if;
  insert into ipl_standings (tournament_id, team_id)
    select p_tid, id from ipl_tournament_teams where tournament_id = p_tid on conflict do nothing;
  update ipl_tournaments set state = 'LEAGUE_RUNNING', league_started_at = now() where id = p_tid;
  return jsonb_build_object('status', 'OK', 'fixtures', cnt, 'teams', n);
end $$;
