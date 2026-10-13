-- ══════════════════════════════════════════════════════════════════════
-- IPL DRAFT MIGRATION V6 — CRITERIA-AWARE OFFER GENERATION & NO ILLEGAL CARDS
-- Apply after migration_ipl_v5.sql. Safe to re-run (idempotent).
-- ══════════════════════════════════════════════════════════════════════

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
  deficit_ids uuid[];
  rem_legal uuid[];
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

  -- Collect all available mathematically legal players for this team from tournament pool
  select array_agg(id) into legal_ids from (
    select pl.player_id as id
      from ipl_tournament_pool pl
      join ipl_players p on p.id = pl.player_id
     where pl.tournament_id = t.id
       and not exists (select 1 from ipl_team_rosters r where r.team_id = p_team_id and r.player_id = pl.player_id)
       and (
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

  ids := '{}'::uuid[];

  -- If criteria is left, guarantee options for each deficit role (up to 4 each)
  if wk_need > 0 then
    select array_agg(id) into deficit_ids from (
      select p.id
        from unnest(legal_ids) lid
        join ipl_players p on p.id = lid
       where p.role = 'WK'
       order by random() limit 4
    ) s;
    if deficit_ids is not null then
      ids := ids || deficit_ids;
    end if;
  end if;

  if b_need > 0 then
    select array_agg(id) into deficit_ids from (
      select p.id
        from unnest(legal_ids) lid
        join ipl_players p on p.id = lid
       where p.role = 'BOWL'
         and not (p.id = any(ids))
       order by random() limit 4
    ) s;
    if deficit_ids is not null then
      ids := ids || deficit_ids;
    end if;
  end if;

  if ar_need > 0 then
    select array_agg(id) into deficit_ids from (
      select p.id
        from unnest(legal_ids) lid
        join ipl_players p on p.id = lid
       where p.role = 'AR'
         and not (p.id = any(ids))
       order by random() limit 4
    ) s;
    if deficit_ids is not null then
      ids := ids || deficit_ids;
    end if;
  end if;

  -- Fill any remaining slots strictly from legal_ids (NEVER include non-legal cards)
  if cardinality(ids) < 11 and cardinality(legal_ids) > cardinality(ids) then
    select array_agg(lid) into rem_legal from (
      select lid
        from unnest(legal_ids) lid
       where not (lid = any(ids))
       order by random() limit (11 - cardinality(ids))
    ) s;
    if rem_legal is not null then
      ids := ids || rem_legal;
    end if;
  end if;

  if ids is null or cardinality(ids) = 0 then
    return jsonb_build_object('status', 'POOL_EXHAUSTED', 'available', 0);
  end if;

  dl := now() + make_interval(secs => t.draft_seconds);
  insert into ipl_draft_offers (tournament_id, team_id, round_no, deadline_at)
  values (t.id, p_team_id, n + 1, dl) returning id into oid;

  insert into ipl_draft_offer_candidates (offer_id, position, player_id)
    select oid, row_number() over (order by random()), x from unnest(ids) x;

  return ipl_offer_json(oid) || jsonb_build_object('status', 'OPEN', 'picks_done', n);
end $$;

-- 4. Update tournament tracking columns updater
create or replace function ipl_update_tournament(p_tid bigint, p_fields jsonb) returns boolean
language plpgsql security definer set search_path = public, ipl_private as $$
begin
  update ipl_tournaments set
    lobby_message_id     = case when p_fields ? 'lobby_message_id'     then (p_fields->>'lobby_message_id')::bigint else lobby_message_id end,
    dashboard_message_id = case when p_fields ? 'dashboard_message_id' then (p_fields->>'dashboard_message_id')::bigint else dashboard_message_id end,
    summary_message_id   = case when p_fields ? 'summary_message_id'   then (p_fields->>'summary_message_id')::bigint else summary_message_id end,
    system_announced     = case when p_fields ? 'system_announced'     then (p_fields->>'system_announced')::boolean else system_announced end,
    final_announced      = case when p_fields ? 'final_announced'      then (p_fields->>'final_announced')::boolean else final_announced end,
    awards_recorded      = case when p_fields ? 'awards_recorded'      then (p_fields->>'awards_recorded')::boolean else awards_recorded end,
    champion_announced   = case when p_fields ? 'champion_announced'   then (p_fields->>'champion_announced')::boolean else champion_announced end,
    mvp_announced        = case when p_fields ? 'mvp_announced'        then (p_fields->>'mvp_announced')::boolean else mvp_announced end,
    final_dms_sent       = case when p_fields ? 'final_dms_sent'       then (p_fields->>'final_dms_sent')::boolean else final_dms_sent end,
    league_summary_posted= case when p_fields ? 'league_summary_posted'then (p_fields->>'league_summary_posted')::boolean else league_summary_posted end,
    league_dms_sent      = case when p_fields ? 'league_dms_sent'      then (p_fields->>'league_dms_sent')::boolean else league_dms_sent end
   where id = p_tid;
  return found;
end $$;

-- 5. Squad summary delivery tracking
alter table ipl_tournament_teams add column if not exists squad_summary_sent boolean not null default false;

create or replace function ipl_mark_squad_summary_sent(p_team_id bigint) returns boolean
language plpgsql security definer set search_path = public, ipl_private as $$
begin
  update ipl_tournament_teams set squad_summary_sent = true where id = p_team_id;
  return found;
end $$;

