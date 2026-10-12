-- ══════════════════════════════════════════════════════════════════════
-- IPL DRAFT MIGRATION V4 — RELIABILITY, LEGALITY & CONCURRENCY
-- Apply after migration_ipl_v3.sql. Safe to re-run (idempotent).
-- ══════════════════════════════════════════════════════════════════════

-- 1. Tracking columns on ipl_tournaments for idempotent finalization and league completion
alter table ipl_tournaments add column if not exists awards_recorded boolean not null default false;
alter table ipl_tournaments add column if not exists champion_announced boolean not null default false;
alter table ipl_tournaments add column if not exists mvp_announced boolean not null default false;
alter table ipl_tournaments add column if not exists final_dms_sent boolean not null default false;
alter table ipl_tournaments add column if not exists league_summary_posted boolean not null default false;
alter table ipl_tournaments add column if not exists league_dms_sent boolean not null default false;

-- 2. Shared Offer Legality Algorithm (prevents 11 illegal cards offer bug)
create or replace function ipl_open_offer(p_team_id bigint, p_force_roles jsonb default null)
returns jsonb language plpgsql security definer set search_path = public, ipl_private as $$
declare
  tm ipl_tournament_teams%rowtype;
  t ipl_tournaments%rowtype;
  o ipl_draft_offers%rowtype;
  n int;
  ids uuid[];
  dl timestamptz;
  oid bigint;
  b_cnt int;
  wk_cnt int;
  ar_cnt int;
  b_need int;
  wk_need int;
  ar_need int;
  tot_needed int;
  picks_left int;
  picks_left_after int;
  legal_ids uuid[];
  other_ids uuid[];
  target_legal int;
  avail_cnt int;
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

  select count(*) into n from ipl_team_rosters where team_id = p_team_id;
  if n >= 11 then return jsonb_build_object('status', 'DONE', 'picks_done', n); end if;

  picks_left := 11 - n;
  picks_left_after := picks_left - 1;

  -- Calculate current roster counts
  select count(*) filter (where p.role = 'BOWL'),
         count(*) filter (where p.role = 'WK'),
         count(*) filter (where p.role = 'AR')
    into b_cnt, wk_cnt, ar_cnt
    from ipl_team_rosters r
    join ipl_players p on p.id = r.player_id
   where r.team_id = p_team_id;

  b_need := greatest(0, 3 - b_cnt);
  wk_need := greatest(0, 1 - wk_cnt);
  ar_need := greatest(0, 1 - ar_cnt);
  tot_needed := b_need + wk_need + ar_need;

  -- Collect all available legal players for this team from tournament pool
  select array_agg(id) into legal_ids from (
    select pl.player_id as id
      from ipl_tournament_pool pl
      join ipl_players p on p.id = pl.player_id
     where pl.tournament_id = t.id
       and not exists (select 1 from ipl_team_rosters r where r.team_id = p_team_id and r.player_id = pl.player_id)
       and (
         -- Feasibility: picking this role leaves enough remaining picks to fulfill outstanding requirements
         picks_left_after >= (
           greatest(0, b_need - (case when p.role = 'BOWL' then 1 else 0 end)) +
           greatest(0, wk_need - (case when p.role = 'WK' then 1 else 0 end)) +
           greatest(0, ar_need - (case when p.role = 'AR' then 1 else 0 end))
         )
       )
     order by random()
  ) s;

  if legal_ids is null or cardinality(legal_ids) = 0 then
    select count(*) into avail_cnt
      from ipl_tournament_pool pl
     where pl.tournament_id = t.id
       and not exists (select 1 from ipl_team_rosters r where r.team_id = p_team_id and r.player_id = pl.player_id);
    return jsonb_build_object('status', 'NO_LEGAL_PLAYERS', 'available', coalesce(avail_cnt, 0));
  end if;

  -- Target at least multiple legal candidates: if in tight crunch (picks_left <= tot_needed), offer all legal up to 11
  target_legal := least(cardinality(legal_ids), case when picks_left <= tot_needed then 11 else 4 end);

  -- Fetch remaining non-legal or other available cards to make total 11 cards
  select array_agg(id) into other_ids from (
    select pl.player_id as id
      from ipl_tournament_pool pl
     where pl.tournament_id = t.id
       and not exists (select 1 from ipl_team_rosters r where r.team_id = p_team_id and r.player_id = pl.player_id)
       and not (pl.player_id = any(legal_ids[1:target_legal]))
     order by random() limit (11 - target_legal)
  ) s;

  ids := legal_ids[1:target_legal] || coalesce(other_ids, '{}'::uuid[]);

  if cardinality(ids) < 11 and cardinality(legal_ids) > target_legal then
    -- Pad with more legal cards if available
    ids := ids || legal_ids[(target_legal + 1):least(cardinality(legal_ids), target_legal + (11 - cardinality(ids)))];
  end if;

  if ids is null or cardinality(ids) < 11 then
    return jsonb_build_object('status', 'POOL_EXHAUSTED', 'available', coalesce(cardinality(ids), 0));
  end if;

  dl := now() + make_interval(secs => t.draft_seconds);
  insert into ipl_draft_offers (tournament_id, team_id, round_no, deadline_at)
  values (t.id, p_team_id, n + 1, dl) returning id into oid;

  insert into ipl_draft_offer_candidates (offer_id, position, player_id)
    select oid, row_number() over (order by random()), x from unnest(ids) x;

  return ipl_offer_json(oid) || jsonb_build_object('status', 'OPEN', 'picks_done', n);
end $$;

-- 3. Database-level Pick Feasibility & Squad Role Constraints
create or replace function ipl_pick(p_offer_id bigint, p_pos int, p_user_id bigint, p_mode text default 'USER',
                                     p_auto_after int default 2, p_grace_seconds int default 3)
returns jsonb language plpgsql security definer set search_path = public, ipl_private as $$
declare
  o ipl_draft_offers%rowtype;
  tm ipl_tournament_teams%rowtype;
  t ipl_tournaments%rowtype;
  pid uuid;
  n int;
  pl ipl_players%rowtype;
  humans_left int;
  b_cnt int;
  wk_cnt int;
  ar_cnt int;
  b_need int;
  wk_need int;
  ar_need int;
  new_tot_needed int;
  picks_left_after int;
begin
  select * into o from ipl_draft_offers where id = p_offer_id for update;
  if not found then return jsonb_build_object('status', 'STALE'); end if;
  select * into tm from ipl_tournament_teams where id = o.team_id;
  select * into t from ipl_tournaments where id = o.tournament_id;

  if p_mode not in ('USER','TIMEOUT','AUTO','SYSTEM') then return jsonb_build_object('status', 'BAD_MODE'); end if;
  if p_mode = 'USER' and (tm.kind <> 'HUMAN' or tm.user_id is distinct from p_user_id) then
    return jsonb_build_object('status', 'NOT_YOURS');
  end if;
  if p_mode = 'SYSTEM' and tm.kind <> 'SYSTEM' then return jsonb_build_object('status', 'NOT_YOURS'); end if;
  if o.status <> 'OPEN' then
    return jsonb_build_object('status', 'ALREADY', 'offer_status', o.status, 'picked_player_id', o.picked_player_id);
  end if;
  if (tm.kind = 'HUMAN' and t.state <> 'DRAFTING') or (tm.kind = 'SYSTEM' and t.state not in ('DRAFTING', 'SYSTEM_TEAM_GENERATION')) then
    return jsonb_build_object('status', 'CLOSED');
  end if;
  if p_mode = 'USER' and now() > o.deadline_at + make_interval(secs => p_grace_seconds) then
    return jsonb_build_object('status', 'EXPIRED');
  end if;
  if p_mode in ('TIMEOUT') and now() < o.deadline_at then
    return jsonb_build_object('status', 'NOT_DUE');
  end if;

  select c.player_id into pid from ipl_draft_offer_candidates c where c.offer_id = o.id and c.position = p_pos;
  if pid is null then return jsonb_build_object('status', 'BAD_POS'); end if;
  select * into pl from ipl_players where id = pid;

  select count(*) into n from ipl_team_rosters where team_id = o.team_id;
  if n >= 11 then return jsonb_build_object('status', 'FULL'); end if;

  picks_left_after := 10 - n;

  -- Strict database validation: verify role feasibility before committing pick
  select count(*) filter (where p.role = 'BOWL'),
         count(*) filter (where p.role = 'WK'),
         count(*) filter (where p.role = 'AR')
    into b_cnt, wk_cnt, ar_cnt
    from ipl_team_rosters r
    join ipl_players p on p.id = r.player_id
   where r.team_id = o.team_id;

  b_need := greatest(0, 3 - b_cnt - (case when pl.role = 'BOWL' then 1 else 0 end));
  wk_need := greatest(0, 1 - wk_cnt - (case when pl.role = 'WK' then 1 else 0 end));
  ar_need := greatest(0, 1 - ar_cnt - (case when pl.role = 'AR' then 1 else 0 end));
  new_tot_needed := b_need + wk_need + ar_need;

  if picks_left_after < new_tot_needed then
    return jsonb_build_object('status', 'ILLEGAL_PICK', 'role', pl.role, 'message', 'Picking this player violates squad role constraints');
  end if;

  insert into ipl_draft_picks (tournament_id, team_id, round_no, player_id, offer_id, auto_picked, reason)
  values (o.tournament_id, o.team_id, o.round_no, pid, o.id, p_mode <> 'USER' and p_mode <> 'SYSTEM', p_mode);

  insert into ipl_team_rosters (tournament_id, team_id, player_id, slot, source)
  values (o.tournament_id, o.team_id, pid, n + 1, case when tm.kind = 'HUMAN' then 'DRAFT' else 'SYSTEM' end);

  delete from ipl_player_reservations where offer_id = o.id;
  update ipl_draft_offers
     set status = case when p_mode in ('USER','SYSTEM') then 'PICKED' else 'AUTO_PICKED' end,
         picked_player_id = pid, resolved_at = now()
   where id = o.id;

  if p_mode = 'USER' then
    update ipl_tournament_teams set timeouts = 0 where id = tm.id;
  elsif p_mode = 'TIMEOUT' then
    update ipl_tournament_teams set timeouts = timeouts + 1,
           auto_draft = auto_draft or (timeouts + 1 >= p_auto_after) where id = tm.id;
  end if;

  if n + 1 = 11 then
    -- Verify squad completeness and role constraints at DB level
    select count(*) filter (where p.role = 'BOWL'),
           count(*) filter (where p.role = 'WK'),
           count(*) filter (where p.role = 'AR')
      into b_cnt, wk_cnt, ar_cnt
      from ipl_team_rosters r
      join ipl_players p on p.id = r.player_id
     where r.team_id = tm.id;

    if b_cnt >= 3 and wk_cnt >= 1 and ar_cnt >= 1 then
      update ipl_tournament_teams set squad_complete = true where id = tm.id;
    else
      raise exception 'Squad complete attempted but role constraints violated for team %: b=%, wk=%, ar=%', tm.id, b_cnt, wk_cnt, ar_cnt;
    end if;
  end if;

  select count(*) into humans_left from ipl_tournament_teams
   where tournament_id = o.tournament_id and kind = 'HUMAN' and not squad_complete;

  return jsonb_build_object('status', 'OK', 'player', ipl_player_json(pl),
                            'picks_done', n + 1, 'squad_complete', n + 1 = 11,
                            'humans_drafting', humans_left);
end $$;

-- 4. Set Failed & Resume Functionality (stores resume_state so /iplresume works)
create or replace function ipl_set_failed(p_tid bigint, p_reason text) returns boolean
language plpgsql security definer set search_path = public, ipl_private as $$
begin
  update ipl_tournaments
     set resume_state = coalesce(resume_state, state),
         state = 'FAILED_RECOVERABLE',
         failure_reason = left(p_reason, 500)
   where id = p_tid and state not in ('LOBBY','COMPLETED','CANCELLED','FAILED_RECOVERABLE');
  return found;
end $$;

create or replace function ipl_resume(p_tid bigint) returns text
language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype; target_state text;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found or t.state <> 'FAILED_RECOVERABLE' then return null; end if;
  target_state := coalesce(t.resume_state, 'LEAGUE_RUNNING');
  update ipl_tournaments
     set state = target_state,
         resume_state = null,
         failure_reason = null
   where id = p_tid;
  return target_state;
end $$;

-- 5. Atomic System Squad Assignment
create or replace function ipl_set_system_squad(p_tid bigint, p_team_id bigint, p_player_ids uuid[])
returns jsonb language plpgsql security definer set search_path = public, ipl_private as $$
declare
  i int;
  pid uuid;
  cnt int;
  b_cnt int;
  wk_cnt int;
  ar_cnt int;
begin
  if cardinality(p_player_ids) <> 11 then
    return jsonb_build_object('status', 'INVALID_COUNT', 'count', cardinality(p_player_ids));
  end if;

  -- Delete existing roster atomically in this transaction
  delete from ipl_team_rosters where team_id = p_team_id;

  -- Insert all 11 players
  for i in 1..11 loop
    pid := p_player_ids[i];
    insert into ipl_team_rosters (tournament_id, team_id, player_id, slot, batting_order, source)
    values (p_tid, p_team_id, pid, i, i, 'SYSTEM');
  end loop;

  -- Verify persisted roster
  select count(*),
         count(*) filter (where p.role = 'BOWL'),
         count(*) filter (where p.role = 'WK'),
         count(*) filter (where p.role = 'AR')
    into cnt, b_cnt, wk_cnt, ar_cnt
    from ipl_team_rosters r
    join ipl_players p on p.id = r.player_id
   where r.team_id = p_team_id;

  if cnt <> 11 or b_cnt < 3 or wk_cnt < 1 or ar_cnt < 1 then
    raise exception 'Persisted squad violates role constraints: cnt=%, bowl=%, wk=%, ar=%', cnt, b_cnt, wk_cnt, ar_cnt;
  end if;

  update ipl_tournament_teams
     set squad_complete = true, batting_order_confirmed = true
   where id = p_team_id;

  return jsonb_build_object('status', 'OK', 'count', cnt, 'bowl', b_cnt, 'wk', wk_cnt, 'ar', ar_cnt);
end $$;

-- 6. Updated ipl_create_fixtures (support 6–16 teams: 4 system + 2..8 human)
create or replace function ipl_create_fixtures(p_tid bigint, p_fixtures jsonb) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare
  t ipl_tournaments%rowtype;
  n int;
  cnt int;
  expected int;
  f jsonb;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found then return jsonb_build_object('status', 'NOT_FOUND'); end if;
  if t.state = 'LEAGUE_RUNNING' or exists (select 1 from ipl_fixtures where tournament_id = p_tid) then
    return jsonb_build_object('status', 'OK', 'already', true);
  end if;
  if t.state not in ('FIXTURE_GENERATION', 'FAILED_RECOVERABLE') then
    return jsonb_build_object('status', 'BAD_STATE', 'state', t.state);
  end if;
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
  if (select count(distinct (least(home_team_id, away_team_id), greatest(home_team_id, away_team_id)))
        from ipl_fixtures where tournament_id = p_tid) <> n * (n - 1) / 2 then
    raise exception 'fixture list does not cover every pair';
  end if;
  insert into ipl_standings (tournament_id, team_id)
  select p_tid, id from ipl_tournament_teams where tournament_id = p_tid
  on conflict (tournament_id, team_id) do nothing;
  update ipl_tournaments set state = 'LEAGUE_RUNNING', league_started_at = now() where id = p_tid;
  return jsonb_build_object('status', 'OK', 'fixtures', cnt, 'teams', n);
end $$;

-- 7. Updated ipl_lease (support leasing COMPLETED tournaments before final announcement)
create or replace function ipl_lease(p_tid bigint, p_owner text, p_seconds int) returns boolean
language plpgsql security definer set search_path = public, ipl_private as $$
begin
  update ipl_tournaments
     set lease_owner = p_owner, lease_until = now() + make_interval(secs => p_seconds)
   where id = p_tid
     and (state not in ('COMPLETED','CANCELLED') or (state = 'COMPLETED' and not coalesce(final_announced, false)))
     and (lease_owner is null or lease_owner = p_owner or lease_until is null or lease_until < now());
  return found;
end $$;

create or replace function ipl_release_lease(p_tid bigint, p_owner text) returns void
language sql security definer set search_path = public, ipl_private as $$
  update ipl_tournaments set lease_owner = null, lease_until = null where id = p_tid and lease_owner = p_owner
$$;


