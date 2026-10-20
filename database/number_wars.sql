-- ════════════════════════════════════════════════════════════════════
--  NUMBER WARS  — run AFTER schema.sql (needs users, groups, games). Idempotent.
--  Supabase → SQL Editor → paste → Run.
-- ════════════════════════════════════════════════════════════════════

create table if not exists nw_matches (
  id               bigint generated always as identity primary key,
  group_id         bigint not null references groups(id),
  host_id          bigint not null references users(id),
  status           text   not null default 'LOBBY'
                     check (status in ('LOBBY','ACTIVE','FINISHED','CANCELLED')),
  min_players      smallint not null default 3,
  max_players      smallint not null default 20,
  current_round    int      not null default 0,
  zero_streak      smallint not null default 0,      -- consecutive rounds where nobody lost HP
  lobby_message_id bigint,
  winner_ids       bigint[] not null default '{}',
  end_reason       text,                              -- LAST_STANDING | ROUND_LIMIT | ALL_ELIMINATED
  announced        boolean  not null default false,
  created_at       timestamptz not null default now(),
  started_at       timestamptz,
  finished_at      timestamptz
);
create index if not exists idx_nw_matches_status on nw_matches(status);
create index if not exists idx_nw_matches_group  on nw_matches(group_id, id desc);
create unique index if not exists uq_nw_one_active_per_group
  on nw_matches(group_id) where status in ('LOBBY','ACTIVE');

create table if not exists nw_players (
  match_id         bigint not null references nw_matches(id) on delete cascade,
  user_id          bigint not null references users(id),
  display_name     text   not null,
  hp               smallint not null default 5 check (hp between 0 and 5),
  alive            boolean  not null default true,
  round_wins       int      not null default 0,
  total_distance   numeric(12,4) not null default 0,
  eliminated_round int,
  joined_at        timestamptz not null default now(),
  primary key (match_id, user_id)
);
create index if not exists idx_nw_players_user on nw_players(user_id, joined_at desc);

create table if not exists nw_rounds (
  id            bigint generated always as identity primary key,
  match_id      bigint not null references nw_matches(id) on delete cascade,
  round_number  int    not null,
  status        text   not null default 'OPEN' check (status in ('OPEN','CLOSED','RESOLVED')),
  multiplier    numeric(4,2) not null default 0.80,   -- stored BEFORE the round starts
  sudden_death  boolean not null default false,
  target        numeric(10,4),
  deadline_at   timestamptz not null,                 -- database clock is the only authority
  resolved_at   timestamptz,
  unique (match_id, round_number)
);
create index if not exists idx_nw_rounds_open on nw_rounds(match_id) where status <> 'RESOLVED';

create table if not exists nw_submissions (
  round_id        bigint not null references nw_rounds(id) on delete cascade,
  user_id         bigint not null references users(id),
  selected_number smallint not null check (selected_number between 1 and 100),
  locked_at       timestamptz not null default now(),
  distance        numeric(10,4),
  hp_delta        smallint,
  is_winner       boolean,
  primary key (round_id, user_id)
);

-- group_id = 0 means GLOBAL (a sentinel avoids NULL-uniqueness problems).
create table if not exists nw_stats (
  user_id        bigint not null references users(id),
  group_id       bigint not null default 0,
  matches_played int not null default 0,
  wins           int not null default 0,
  losses         int not null default 0,
  round_wins     int not null default 0,
  rating         int not null default 1000,
  updated_at     timestamptz not null default now(),
  primary key (user_id, group_id)
);
create index if not exists idx_nw_stats_rank on nw_stats(group_id, rating desc, wins desc);

-- ── one game (of ANY kind) per group ────────────────────────────────
-- Each table already has a unique index for its own kind; these triggers add the
-- cross-check. Both take the same advisory lock, so two /start presses racing for
-- different games are serialised and the loser gets a unique-violation (23505).
create or replace function nw_guard_new_match() returns trigger language plpgsql as $$
begin
  perform pg_advisory_xact_lock(new.group_id);
  if exists (select 1 from games where group_id = new.group_id
              and status in ('LOBBY','DRAFTING','LEAGUE','FINAL')) then
    raise exception 'group already has an active game' using errcode = '23505';
  end if;
  return new;
end $$;
drop trigger if exists trg_nw_guard on nw_matches;
create trigger trg_nw_guard before insert on nw_matches
  for each row execute function nw_guard_new_match();

create or replace function games_guard_new_game() returns trigger language plpgsql as $$
begin
  perform pg_advisory_xact_lock(new.group_id);
  if exists (select 1 from nw_matches where group_id = new.group_id
              and status in ('LOBBY','ACTIVE')) then
    raise exception 'group already has an active game' using errcode = '23505';
  end if;
  return new;
end $$;
drop trigger if exists trg_games_guard on games;
create trigger trg_games_guard before insert on games
  for each row execute function games_guard_new_game();

-- ── lobby ───────────────────────────────────────────────────────────
create or replace function nw_join(p_match_id bigint, p_user_id bigint, p_name text)
returns text language plpgsql as $$
declare m nw_matches%rowtype; cnt int;
begin
  select * into m from nw_matches where id = p_match_id for update;
  if not found or m.status <> 'LOBBY' then return 'CLOSED'; end if;
  if exists (select 1 from nw_players where match_id = p_match_id and user_id = p_user_id) then
    return 'ALREADY';
  end if;
  if exists (
    select 1 from nw_players np
    join nw_matches m2 on m2.id = np.match_id
    where np.user_id = p_user_id and m2.id <> p_match_id and m2.status in ('LOBBY', 'ACTIVE')
  ) then
    return 'OTHER_MATCH';
  end if;
  select count(*) into cnt from nw_players where match_id = p_match_id;
  if cnt >= m.max_players then return 'FULL'; end if;
  insert into nw_players (match_id, user_id, display_name) values (p_match_id, p_user_id, p_name);
  return 'OK';
end $$;

create or replace function nw_leave(p_match_id bigint, p_user_id bigint)
returns text language plpgsql as $$
declare m nw_matches%rowtype;
begin
  select * into m from nw_matches where id = p_match_id for update;
  if not found or m.status <> 'LOBBY' then return 'CLOSED'; end if;
  delete from nw_players where match_id = p_match_id and user_id = p_user_id;
  if not found then return 'NOT_IN'; end if;
  return 'OK';
end $$;

create or replace function nw_start(p_match_id bigint)
returns boolean language plpgsql as $$
declare m nw_matches%rowtype; cnt int;
begin
  select * into m from nw_matches where id = p_match_id for update;
  if not found or m.status <> 'LOBBY' then return false; end if;
  select count(*) into cnt from nw_players where match_id = p_match_id;
  if cnt < m.min_players then return false; end if;
  update nw_matches set status = 'ACTIVE', started_at = now() where id = p_match_id;
  return true;
end $$;

-- ── rounds ──────────────────────────────────────────────────────────
-- Opens the next round, or returns the unresolved one (restart recovery is idempotent).
create or replace function nw_open_round(p_match_id bigint, p_seconds int)
returns jsonb language plpgsql as $$
declare m nw_matches%rowtype; r nw_rounds%rowtype; mult numeric; sd boolean;
begin
  select * into m from nw_matches where id = p_match_id for update;
  if not found or m.status <> 'ACTIVE' then return null; end if;

  select * into r from nw_rounds
   where match_id = p_match_id and status <> 'RESOLVED'
   order by round_number desc limit 1;
  if found then
    return jsonb_build_object('created', false, 'round', to_jsonb(r),
             'seconds_left', greatest(0, extract(epoch from (r.deadline_at - now()))));
  end if;

  sd   := m.zero_streak >= 3;     -- three zero-damage rounds in a row → sudden death
  mult := case when sd then (array[0.5, 0.8, 1.2, 1.5])[1 + floor(random() * 4)::int] else 0.8 end;
  insert into nw_rounds (match_id, round_number, multiplier, sudden_death, deadline_at)
  values (p_match_id, m.current_round + 1, mult, sd, now() + make_interval(secs => p_seconds))
  returning * into r;
  update nw_matches set current_round = r.round_number where id = p_match_id;
  return jsonb_build_object('created', true, 'round', to_jsonb(r),
           'seconds_left', greatest(0, extract(epoch from (r.deadline_at - now()))));
end $$;

-- A player's round that is currently open (used by "type a number" in DM).
create or replace function nw_open_round_for_user(p_user_id bigint)
returns bigint language sql stable as $$
  select r.id
    from nw_rounds r
    join nw_matches m on m.id = r.match_id and m.status = 'ACTIVE'
    join nw_players p on p.match_id = r.match_id and p.user_id = p_user_id and p.alive
   where r.status = 'OPEN' and r.deadline_at > now()
   order by r.id desc limit 1
$$;

-- Lock a number. FOR SHARE on the round row serialises against nw_close_round (FOR UPDATE),
-- so a submission either lands before the round closes or is rejected — never in between.
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

-- Close a round once its (database-clock) deadline passed or everyone has locked in.
create or replace function nw_close_round(p_round_id bigint)
returns text language plpgsql as $$
declare r nw_rounds%rowtype; alive_cnt int; sub_cnt int;
begin
  select * into r from nw_rounds where id = p_round_id for update;
  if not found then return 'MISSING'; end if;
  if r.status = 'RESOLVED' then return 'RESOLVED'; end if;
  if r.status = 'CLOSED'   then return 'CLOSED';   end if;
  select count(*) into alive_cnt from nw_players where match_id = r.match_id and alive;
  select count(*) into sub_cnt   from nw_submissions where round_id = p_round_id;
  if now() >= r.deadline_at or sub_cnt >= alive_cnt then
    update nw_rounds set status = 'CLOSED' where id = p_round_id;
    return 'CLOSED';
  end if;
  return 'OPEN';
end $$;

-- Apply a round's results ONCE, atomically (HP, wins, distance, elimination, streak and — if the
-- match ended — the final result and rating changes). A second call returns false: idempotent.
create or replace function nw_apply_round(
  p_round_id bigint, p_target numeric, p_results jsonb, p_zero_damage boolean,
  p_winner_ids bigint[], p_end_reason text)
returns boolean language plpgsql as $$
declare r nw_rounds%rowtype; mstat text; x record;
begin
  select * into r from nw_rounds where id = p_round_id for update;
  if not found or r.status <> 'CLOSED' then return false; end if;
  select status into mstat from nw_matches where id = r.match_id;
  if mstat <> 'ACTIVE' then return false; end if;

  for x in
    select * from jsonb_to_recordset(p_results)
      as t(user_id bigint, number int, distance numeric, hp_delta int, is_winner boolean)
  loop
    if x.number is not null then
      update nw_submissions
         set distance = x.distance, hp_delta = x.hp_delta, is_winner = x.is_winner
       where round_id = p_round_id and user_id = x.user_id;
    end if;
    update nw_players set
      hp               = greatest(0, least(5, hp + x.hp_delta)),
      alive            = greatest(0, least(5, hp + x.hp_delta)) > 0,
      round_wins       = round_wins + (case when x.is_winner then 1 else 0 end),
      total_distance   = total_distance + coalesce(x.distance, 0),
      eliminated_round = case when hp + x.hp_delta <= 0 then r.round_number else eliminated_round end
    where match_id = r.match_id and user_id = x.user_id and alive;
  end loop;

  update nw_rounds set status = 'RESOLVED', target = p_target, resolved_at = now()
   where id = p_round_id;
  update nw_matches set zero_streak =
      case when r.sudden_death then 0
           when p_zero_damage  then zero_streak + 1
           else 0 end
   where id = r.match_id;

  if p_end_reason is not null then
    perform nw_finish_internal(r.match_id, p_winner_ids, p_end_reason);
  end if;
  return true;
end $$;

-- Rating: 1000 start · +25 outright win · +10 shared title · −5 completed loss · cancelled = 0.
create or replace function nw_finish_internal(p_match_id bigint, p_winners bigint[], p_reason text)
returns void language plpgsql as $$
declare m nw_matches%rowtype; p record; is_win boolean; d int; n_win int;
begin
  select * into m from nw_matches where id = p_match_id for update;
  if m.status <> 'ACTIVE' then return; end if;
  n_win := coalesce(array_length(p_winners, 1), 0);
  update nw_matches
     set status = 'FINISHED', winner_ids = p_winners, end_reason = p_reason, finished_at = now()
   where id = p_match_id;

  for p in select * from nw_players where match_id = p_match_id loop
    is_win := p.user_id = any(p_winners);
    d := case when is_win and n_win = 1 then 25 when is_win then 10 else -5 end;
    insert into nw_stats (user_id, group_id, matches_played, wins, losses, round_wins, rating)
    select p.user_id, s.gid, 1,
           case when is_win then 1 else 0 end, case when is_win then 0 else 1 end,
           p.round_wins, 1000 + d
      from unnest(array[0::bigint, m.group_id]) as s(gid)
    on conflict (user_id, group_id) do update set
      matches_played = nw_stats.matches_played + 1,
      wins           = nw_stats.wins + excluded.wins,
      losses         = nw_stats.losses + excluded.losses,
      round_wins     = nw_stats.round_wins + excluded.round_wins,
      rating         = nw_stats.rating + d,
      updated_at     = now();
  end loop;
end $$;

create or replace function nw_claim_announce(p_match_id bigint)
returns boolean language plpgsql as $$
begin
  update nw_matches set announced = true
   where id = p_match_id and announced = false and status = 'FINISHED';
  return found;
end $$;

-- ── security: same model as the rest of the schema (service_role only) ──
alter table nw_matches     enable row level security;
alter table nw_players     enable row level security;
alter table nw_rounds      enable row level security;
alter table nw_submissions enable row level security;
alter table nw_stats       enable row level security;

revoke all on function nw_join(bigint, bigint, text)                     from public, anon, authenticated;
revoke all on function nw_leave(bigint, bigint)                          from public, anon, authenticated;
revoke all on function nw_start(bigint)                                  from public, anon, authenticated;
revoke all on function nw_open_round(bigint, int)                        from public, anon, authenticated;
revoke all on function nw_open_round_for_user(bigint)                    from public, anon, authenticated;
revoke all on function nw_submit(bigint, bigint, int)                    from public, anon, authenticated;
revoke all on function nw_close_round(bigint)                            from public, anon, authenticated;
revoke all on function nw_apply_round(bigint, numeric, jsonb, boolean, bigint[], text) from public, anon, authenticated;
revoke all on function nw_finish_internal(bigint, bigint[], text)        from public, anon, authenticated;
revoke all on function nw_claim_announce(bigint)                         from public, anon, authenticated;
grant execute on function nw_join(bigint, bigint, text)                     to service_role;
grant execute on function nw_leave(bigint, bigint)                          to service_role;
grant execute on function nw_start(bigint)                                  to service_role;
grant execute on function nw_open_round(bigint, int)                        to service_role;
grant execute on function nw_open_round_for_user(bigint)                    to service_role;
grant execute on function nw_submit(bigint, bigint, int)                    to service_role;
grant execute on function nw_close_round(bigint)                            to service_role;
grant execute on function nw_apply_round(bigint, numeric, jsonb, boolean, bigint[], text) to service_role;
grant execute on function nw_finish_internal(bigint, bigint[], text)        to service_role;
grant execute on function nw_claim_announce(bigint)                         to service_role;

-- Migration for existing tables to ensure MAX_HP = 5:
update nw_players set hp = 5 where hp > 5;
alter table if exists nw_players alter column hp set default 5;
alter table if exists nw_players drop constraint if exists nw_players_hp_check;
alter table if exists nw_players add constraint nw_players_hp_check check (hp between 0 and 5);
