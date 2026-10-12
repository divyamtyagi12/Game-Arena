-- ══════════════════════════════════════════════════════════════════════
-- IPL DRAFT MIGRATION V5 — UNIFIED CONTRACT, LEGALITY & LEASE SAFETY
-- Apply after migration_ipl_v4.sql. Safe to re-run (idempotent).
-- ══════════════════════════════════════════════════════════════════════

-- 1. Unified ipl_pick response contract, duplicate protection & role constraints
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
    return jsonb_build_object('status', 'ALREADY', 'offer_status', o.status, 'picked_player_id', o.picked_player_id,
                              'team_id', tm.id, 'tournament_id', o.tournament_id, 'round_no', o.round_no,
                              'done', (select count(*) from ipl_team_rosters where team_id = tm.id) >= 11);
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

  -- Disallow duplicate player within the same squad
  if exists (select 1 from ipl_team_rosters where team_id = o.team_id and player_id = pid) then
    return jsonb_build_object('status', 'ALREADY_HAVE', 'message', 'Player is already in your squad');
  end if;

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

  return jsonb_build_object(
    'status', 'OK',
    'player', ipl_player_json(pl),
    'round_no', o.round_no,
    'picks_done', n + 1,
    'done', n + 1 = 11,
    'squad_complete', n + 1 = 11,
    'team_id', tm.id,
    'user_id', tm.user_id,
    'tournament_id', o.tournament_id,
    'all_humans_done', humans_left = 0,
    'humans_drafting', humans_left,
    'auto', p_mode in ('TIMEOUT','AUTO')
  );
end $$;

-- 2. Ensure ipl_create_fixtures correctly transitions tournament state to LEAGUE_RUNNING
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
  if t.state = 'LEAGUE_RUNNING' then
    return jsonb_build_object('status', 'OK', 'already', true);
  end if;
  if exists (select 1 from ipl_fixtures where tournament_id = p_tid) then
    update ipl_tournaments set state = 'LEAGUE_RUNNING', league_started_at = coalesce(league_started_at, now()) where id = p_tid;
    return jsonb_build_object('status', 'OK', 'already', true);
  end if;
  if t.state not in ('FIXTURE_GENERATION', 'FAILED_RECOVERABLE') then
    return jsonb_build_object('status', 'BAD_STATE', 'state', t.state);
  end if;
  select count(*) into n from ipl_tournament_teams where tournament_id = p_tid;
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

-- 3. Atomic Lease Ownership and Expiry Handling
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
  update ipl_tournaments set lease_owner = null, lease_until = null where id = p_tid and lease_owner = p_owner;
$$;

create or replace function ipl_unannounced_completed() returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(ipl_tournament_json(t) order by t.id), '[]'::jsonb)
    from ipl_tournaments t where t.state = 'COMPLETED' and not coalesce(t.final_announced, false);
$$;
