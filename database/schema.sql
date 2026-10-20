-- ════════════════════════════════════════════════════════════════════
--  ANIME DRAFT — Supabase / PostgreSQL schema
--  Paste this whole file into: Supabase Dashboard → SQL Editor → Run.
--  Idempotent: safe to run again.
--  The bot connects with the *service_role* key; RLS is enabled with NO
--  policies so the public anon key can read/write nothing.
-- ════════════════════════════════════════════════════════════════════

-- ───────────── users / groups ─────────────
create table if not exists users (
  id          bigint primary key,                 -- Telegram user id
  username    text,
  first_name  text        not null default '',
  dm_started  boolean     not null default false, -- has opened the bot privately
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create table if not exists groups (
  id          bigint primary key,                 -- Telegram chat id
  title       text,
  created_at  timestamptz not null default now()
);

-- ───────────── characters ─────────────
create table if not exists characters (
  id             text primary key,                -- slug, e.g. 'reinhard'
  name           text not null,
  series         text not null check (series in ('Re:ZERO', 'Black Clover', 'Death Note', 'Ben 10')),
  image_url      text,
  image_file_id  text,                            -- cached Telegram file_id
  attack         smallint not null check (attack between 1 and 100),
  tanking        smallint not null check (tanking between 1 and 100),
  speed          smallint not null check (speed between 1 and 100),
  healing        smallint not null check (healing between 1 and 100),
  intelligence   smallint not null check (intelligence between 1 and 100),
  rarity         text not null check (rarity in ('COMMON', 'RARE', 'EPIC', 'LEGENDARY')),
  abilities      jsonb not null default '[]'::jsonb,
  description    text not null default '',
  active         boolean not null default true
);

-- Migration for databases created before a franchise was added: refresh the series check.
alter table characters drop constraint if exists characters_series_check;
alter table characters add constraint characters_series_check
  check (series in ('Re:ZERO', 'Black Clover', 'Death Note', 'Ben 10'));

create table if not exists character_categories (
  character_id  text not null references characters(id) on delete cascade,
  category      text not null check (category in
                  ('ATTACK', 'TANKING', 'SPEED', 'HEALING', 'INTELLIGENCE')),
  primary key (character_id, category)
);
create index if not exists idx_character_categories_category on character_categories(category);

-- Migration for databases created when 'Defense' still existed (safe to re-run).
delete from character_categories where category = 'DEFENSE';
alter table character_categories drop constraint if exists character_categories_category_check;
alter table character_categories add constraint character_categories_category_check
  check (category in ('ATTACK', 'TANKING', 'SPEED', 'HEALING', 'INTELLIGENCE'));
alter table characters drop column if exists defense;

-- ───────────── games ─────────────
create table if not exists games (
  id                  bigint generated always as identity primary key,
  group_id            bigint not null references groups(id),
  host_id             bigint not null references users(id),
  status              text   not null default 'LOBBY'
                        check (status in ('LOBBY','DRAFTING','LEAGUE','FINAL','COMPLETED','CANCELLED')),
  min_players         smallint not null default 3,
  max_players         smallint not null default 8,
  lobby_message_id    bigint,
  progress_message_id bigint,
  table_message_id    bigint,
  champion_user_id    bigint references users(id),
  final_announced     boolean not null default false,
  draft_deadline      timestamptz,
  created_at          timestamptz not null default now(),
  started_at          timestamptz,
  completed_at        timestamptz
);
create index if not exists idx_games_status on games(status);
create index if not exists idx_games_group  on games(group_id, id desc);
-- Only ONE unfinished game per group (prevents duplicate lobbies under races)
create unique index if not exists uq_games_one_active_per_group
  on games(group_id) where status in ('LOBBY','DRAFTING','LEAGUE','FINAL');

create table if not exists game_players (
  id            bigint generated always as identity primary key,
  game_id       bigint not null references games(id) on delete cascade,
  user_id       bigint not null references users(id),
  display_name  text   not null,
  draft_done    boolean not null default false,
  joined_at     timestamptz not null default now(),
  unique (game_id, user_id)                       -- no duplicate joins
);
create index if not exists idx_game_players_game on game_players(game_id);
create index if not exists idx_game_players_user on game_players(user_id, id desc);

-- ───────────── drafting ─────────────
-- The five characters offered to a player for a category (kept so a restart
-- shows the same offer and old buttons keep working).
create table if not exists draft_offers (
  id              bigint generated always as identity primary key,
  game_player_id  bigint not null references game_players(id) on delete cascade,
  category        text   not null,
  character_ids   text[] not null,
  created_at      timestamptz not null default now(),
  unique (game_player_id, category)
);

create table if not exists draft_choices (
  id              bigint generated always as identity primary key,
  game_player_id  bigint not null references game_players(id) on delete cascade,
  category        text   not null,
  character_id    text   not null references characters(id),
  auto_picked     boolean not null default false,
  picked_at       timestamptz not null default now(),
  unique (game_player_id, category),              -- one pick per category (no double click)
  unique (game_player_id, character_id)           -- same character can't fill two slots
);
create index if not exists idx_draft_choices_player on draft_choices(game_player_id);

create table if not exists teams (
  id              bigint generated always as identity primary key,
  game_player_id  bigint not null unique references game_players(id) on delete cascade,
  attack_id       text not null references characters(id),
  tanking_id      text not null references characters(id),
  speed_id        text not null references characters(id),
  healing_id      text not null references characters(id),
  intelligence_id text not null references characters(id),
  locked_at       timestamptz not null default now()
);

-- ───────────── matches ─────────────
create table if not exists matches (
  id            bigint generated always as identity primary key,
  game_id       bigint not null references games(id) on delete cascade,
  stage         text   not null check (stage in ('LEAGUE','FINAL')),
  round_no      smallint not null default 0,
  p1_id         bigint not null references game_players(id),
  p2_id         bigint not null references game_players(id),
  status        text   not null default 'PENDING' check (status in ('PENDING','RUNNING','DONE')),
  p1_score      smallint,
  p2_score      smallint,
  winner_id     bigint references game_players(id),   -- null = draw
  tiebreak_used boolean not null default false,
  tiebreak_p1   numeric(8,3),
  tiebreak_p2   numeric(8,3),
  notified      boolean not null default false,        -- scorecards delivered to DMs
  started_at    timestamptz,
  finished_at   timestamptz,
  check (p1_id <> p2_id),
  unique (game_id, stage, p1_id, p2_id)
);
create index if not exists idx_matches_game on matches(game_id, stage, round_no);
create unique index if not exists uq_matches_one_final on matches(game_id) where stage = 'FINAL';

create table if not exists match_clashes (
  id               bigint generated always as identity primary key,
  match_id         bigint not null references matches(id) on delete cascade,
  clash_no         smallint not null,
  category         text not null,
  p1_character_id  text not null references characters(id),
  p2_character_id  text not null references characters(id),
  p1_rating        smallint not null,
  p2_rating        smallint not null,
  p1_score         numeric(8,3) not null,
  p2_score         numeric(8,3) not null,
  winner_side      smallint not null check (winner_side in (1, 2)),
  unique (match_id, category)
);
create index if not exists idx_match_clashes_match on match_clashes(match_id);

create table if not exists standings (
  id               bigint generated always as identity primary key,
  game_id          bigint not null references games(id) on delete cascade,
  game_player_id   bigint not null references game_players(id) on delete cascade,
  played           integer not null default 0,
  wins             integer not null default 0,
  draws            integer not null default 0,
  losses           integer not null default 0,
  clashes_won      integer not null default 0,
  clashes_lost     integer not null default 0,
  clash_difference integer generated always as (clashes_won - clashes_lost) stored,
  points           integer not null default 0,
  unique (game_id, game_player_id)
);
create index if not exists idx_standings_rank
  on standings(game_id, points desc, clash_difference desc, clashes_won desc);

-- Migration (continued): drop Defense leftovers from old databases (safe to re-run).
delete from draft_offers  where category = 'DEFENSE';
delete from draft_choices where category = 'DEFENSE';
delete from match_clashes where category = 'DEFENSE';
alter table teams drop column if exists defense_id;

-- ════════════════ atomic game operations (called via RPC) ════════════════
-- Row locks (FOR UPDATE) make these safe under simultaneous button presses.

create or replace function join_game(p_game_id bigint, p_user_id bigint, p_name text)
returns text language plpgsql as $$
declare g games%rowtype; cnt int;
begin
  select * into g from games where id = p_game_id for update;
  if not found or g.status <> 'LOBBY' then return 'CLOSED'; end if;
  if exists (select 1 from game_players where game_id = p_game_id and user_id = p_user_id) then
    return 'ALREADY';
  end if;
  if exists (
    select 1 from game_players gp
    join games g2 on g2.id = gp.game_id
    where gp.user_id = p_user_id and g2.id <> p_game_id and g2.status in ('LOBBY', 'DRAFTING', 'LEAGUE', 'FINAL')
  ) then
    return 'OTHER_GAME';
  end if;
  select count(*) into cnt from game_players where game_id = p_game_id;
  if cnt >= g.max_players then return 'FULL'; end if;
  insert into game_players (game_id, user_id, display_name) values (p_game_id, p_user_id, p_name);
  return 'OK';
end $$;

create or replace function leave_game(p_game_id bigint, p_user_id bigint)
returns text language plpgsql as $$
declare g games%rowtype;
begin
  select * into g from games where id = p_game_id for update;
  if not found or g.status <> 'LOBBY' then return 'CLOSED'; end if;
  delete from game_players where game_id = p_game_id and user_id = p_user_id;
  if found then return 'OK'; end if;
  return 'NOT_IN';
end $$;

-- LOBBY -> DRAFTING (exactly one caller wins; others get false)
create or replace function begin_draft(p_game_id bigint, p_deadline timestamptz)
returns boolean language plpgsql as $$
declare g games%rowtype; cnt int;
begin
  select * into g from games where id = p_game_id for update;
  if not found or g.status <> 'LOBBY' then return false; end if;
  select count(*) into cnt from game_players where game_id = p_game_id;
  if cnt < g.min_players then return false; end if;
  update games set status = 'DRAFTING', started_at = now(), draft_deadline = p_deadline
   where id = p_game_id;
  return true;
end $$;

-- DRAFTING -> LEAGUE once every player is done (exactly one caller wins)
create or replace function begin_league(p_game_id bigint)
returns boolean language plpgsql as $$
declare g games%rowtype;
begin
  select * into g from games where id = p_game_id for update;
  if not found or g.status <> 'DRAFTING' then return false; end if;
  if exists (select 1 from game_players where game_id = p_game_id and not draft_done) then
    return false;
  end if;
  update games set status = 'LEAGUE' where id = p_game_id;
  insert into standings (game_id, game_player_id)
    select game_id, id from game_players where game_id = p_game_id
  on conflict do nothing;
  return true;
end $$;

-- LEAGUE -> FINAL; creates the single final match. Idempotent for recovery.
create or replace function begin_final(p_game_id bigint, p_p1 bigint, p_p2 bigint)
returns jsonb language plpgsql as $$
declare g games%rowtype; mid bigint;
begin
  select * into g from games where id = p_game_id for update;
  if not found then return null; end if;
  if g.status = 'FINAL' then
    select id into mid from matches where game_id = p_game_id and stage = 'FINAL';
    return jsonb_build_object('match_id', mid, 'created', false);
  end if;
  if g.status <> 'LEAGUE' then return null; end if;
  if exists (select 1 from matches where game_id = p_game_id and stage = 'LEAGUE' and status <> 'DONE') then
    return null;
  end if;
  update games set status = 'FINAL' where id = p_game_id;
  insert into matches (game_id, stage, round_no, p1_id, p2_id)
    values (p_game_id, 'FINAL', 0, p_p1, p_p2) returning id into mid;
  return jsonb_build_object('match_id', mid, 'created', true);
end $$;

-- Persist a finished match + award points EXACTLY once.
-- p_winner_side: 1 / 2 / 0 (draw, league only).
create or replace function finish_match(
  p_match_id bigint, p_clashes jsonb, p_winner_side int,
  p_tb1 numeric default null, p_tb2 numeric default null
) returns boolean language plpgsql as $$
declare m matches%rowtype; s1 int; s2 int; wid bigint;
begin
  select * into m from matches where id = p_match_id for update;
  if not found or m.status = 'DONE' then return false; end if;
  if m.stage = 'FINAL' and p_winner_side not in (1, 2) then
    raise exception 'A final needs a winner';
  end if;

  insert into match_clashes
    (match_id, clash_no, category, p1_character_id, p2_character_id,
     p1_rating, p2_rating, p1_score, p2_score, winner_side)
  select p_match_id, x.clash_no, x.category, x.p1_character_id, x.p2_character_id,
         x.p1_rating, x.p2_rating, x.p1_score, x.p2_score, x.winner_side
    from jsonb_to_recordset(p_clashes) as x(
      clash_no int, category text, p1_character_id text, p2_character_id text,
      p1_rating int, p2_rating int, p1_score numeric, p2_score numeric, winner_side int);

  select count(*) filter (where winner_side = 1), count(*) filter (where winner_side = 2)
    into s1, s2 from match_clashes where match_id = p_match_id;

  wid := case p_winner_side when 1 then m.p1_id when 2 then m.p2_id else null end;

  update matches set status = 'DONE', p1_score = s1, p2_score = s2, winner_id = wid,
         tiebreak_used = (p_tb1 is not null), tiebreak_p1 = p_tb1, tiebreak_p2 = p_tb2,
         finished_at = now()
   where id = p_match_id;

  if m.stage = 'LEAGUE' then
    update standings set
      played = played + 1,
      wins   = wins   + (case when p_winner_side = 1 then 1 else 0 end),
      draws  = draws  + (case when p_winner_side = 0 then 1 else 0 end),
      losses = losses + (case when p_winner_side = 2 then 1 else 0 end),
      clashes_won  = clashes_won  + s1,
      clashes_lost = clashes_lost + s2,
      points = points + (case p_winner_side when 1 then 3 when 0 then 1 else 0 end)
     where game_player_id = m.p1_id;
    update standings set
      played = played + 1,
      wins   = wins   + (case when p_winner_side = 2 then 1 else 0 end),
      draws  = draws  + (case when p_winner_side = 0 then 1 else 0 end),
      losses = losses + (case when p_winner_side = 1 then 1 else 0 end),
      clashes_won  = clashes_won  + s2,
      clashes_lost = clashes_lost + s1,
      points = points + (case p_winner_side when 2 then 3 when 0 then 1 else 0 end)
     where game_player_id = m.p2_id;
  else
    update games set status = 'COMPLETED', completed_at = now(),
           champion_user_id = (select user_id from game_players where id = wid)
     where id = m.game_id and status = 'FINAL';
  end if;
  return true;
end $$;

-- ════════════════ security: RLS on, RPCs only for service_role ════════════════
alter table users                enable row level security;
alter table groups               enable row level security;
alter table characters           enable row level security;
alter table character_categories enable row level security;
alter table games                enable row level security;
alter table game_players         enable row level security;
alter table draft_offers         enable row level security;
alter table draft_choices        enable row level security;
alter table teams                enable row level security;
alter table matches              enable row level security;
alter table match_clashes        enable row level security;
alter table standings            enable row level security;

revoke all on function join_game(bigint, bigint, text)            from public, anon, authenticated;
revoke all on function leave_game(bigint, bigint)                 from public, anon, authenticated;
revoke all on function begin_draft(bigint, timestamptz)           from public, anon, authenticated;
revoke all on function begin_league(bigint)                       from public, anon, authenticated;
revoke all on function begin_final(bigint, bigint, bigint)        from public, anon, authenticated;
revoke all on function finish_match(bigint, jsonb, int, numeric, numeric) from public, anon, authenticated;
grant execute on function join_game(bigint, bigint, text)            to service_role;
grant execute on function leave_game(bigint, bigint)                 to service_role;
grant execute on function begin_draft(bigint, timestamptz)           to service_role;
grant execute on function begin_league(bigint)                       to service_role;
grant execute on function begin_final(bigint, bigint, bigint)        to service_role;
grant execute on function finish_match(bigint, jsonb, int, numeric, numeric) to service_role;


-- ════════════════════════════════════════════════════════════════════
--  LEADERBOARD  (global Elo rating + per-group stats)
--  Idempotent: safe to re-run.
-- ════════════════════════════════════════════════════════════════════

-- games.leaderboard_processed = "results of this game are already in the stats".
-- Games that finished BEFORE the leaderboard existed are marked processed so they
-- don't silently change ratings. To count them, run once, then restart the bot:
--     update games set leaderboard_processed = false where status = 'COMPLETED';
do $$
begin
  if not exists (select 1 from information_schema.columns
                  where table_schema = current_schema()
                    and table_name = 'games' and column_name = 'leaderboard_processed') then
    alter table games add column leaderboard_processed boolean not null default false;
    update games set leaderboard_processed = true where status in ('COMPLETED', 'CANCELLED');
  end if;
end $$;

create table if not exists player_global_stats (
  user_id             bigint primary key references users(id),     -- Telegram user id
  rating              integer not null default 1000,
  tournaments_played  integer not null default 0,
  championships_won   integer not null default 0,
  runner_up_finishes  integer not null default 0,
  matches_won         integer not null default 0,                  -- league matches
  matches_lost        integer not null default 0,
  matches_drawn       integer not null default 0,
  clashes_won         integer not null default 0,                  -- league + final clashes
  clashes_lost        integer not null default 0,
  updated_at          timestamptz not null default now()
);
-- Serves "top N", "my rank" (count of strictly better rows) and pagination.
create index if not exists idx_pgs_rank
  on player_global_stats (rating desc, championships_won desc, runner_up_finishes desc, user_id);

create table if not exists player_group_stats (
  user_id             bigint not null references users(id),
  group_id            bigint not null references groups(id),       -- Telegram chat id
  tournaments_played  integer not null default 0,
  championships_won   integer not null default 0,
  runner_up_finishes  integer not null default 0,
  matches_won         integer not null default 0,
  matches_lost        integer not null default 0,
  matches_drawn       integer not null default 0,
  clashes_won         integer not null default 0,
  clashes_lost        integer not null default 0,
  updated_at          timestamptz not null default now(),
  primary key (group_id, user_id)                                  -- one row per player per group
);
create index if not exists idx_pgrs_user  on player_group_stats (user_id, updated_at desc);
create index if not exists idx_pgrs_champs
  on player_group_stats (group_id, championships_won desc, runner_up_finishes desc, user_id);
create index if not exists idx_pgrs_wins
  on player_group_stats (group_id, matches_won desc, clashes_won desc, user_id);

create table if not exists rating_history (
  id             bigint generated always as identity primary key,
  user_id        bigint  not null references users(id),
  game_id        bigint  not null references games(id),
  old_rating     integer not null,
  new_rating     integer not null,
  rating_change  integer not null,
  created_at     timestamptz not null default now(),
  unique (user_id, game_id)                       -- a tournament can never reward a player twice
);
create index if not exists idx_rating_history_game on rating_history (game_id);
create index if not exists idx_rating_history_user on rating_history (user_id, id desc);

-- ───────────── recording a finished tournament (atomic + idempotent) ─────────────
-- p_placements: Telegram user ids, 1st place first (champion, runner-up, then the
-- remaining players in final league-table order). No ties: the league table already
-- breaks every tie deterministically.
--
-- Rating formula (pairwise Elo, "multiplayer Elo"):
--   for every pair (me, opp) in the tournament
--       S = 1 if I placed above opp else 0
--       E = 1 / (1 + 10 ^ ((R_opp - R_me) / 400))
--   change = round( K / (N - 1) * Σ (S - E) )          K = 32, N = number of players
-- Beating a stronger player earns more (S - E is large), losing to a weaker one costs
-- more (E is large). All ratings used are the values BEFORE this tournament.
create or replace function record_game_results(
  p_game_id bigint, p_placements bigint[], p_k numeric default 32
) returns jsonb language plpgsql as $$
declare
  g games%rowtype;
  v_n int;
begin
  select * into g from games where id = p_game_id for update;       -- serialises per game
  if not found or g.status <> 'COMPLETED' then
    return jsonb_build_object('status', 'NOT_COMPLETED');            -- cancelled / unfinished never count
  end if;
  if g.leaderboard_processed then
    return jsonb_build_object('status', 'ALREADY');                  -- idempotent
  end if;

  v_n := coalesce(array_length(p_placements, 1), 0);
  if v_n < 2
     or (select count(distinct x) from unnest(p_placements) as x) <> v_n
     or (select count(*) from game_players where game_id = p_game_id) <> v_n
     or exists (select 1 from game_players gp
                 where gp.game_id = p_game_id and gp.user_id <> all (p_placements))
     or g.champion_user_id is distinct from p_placements[1] then
    raise exception 'record_game_results: placements do not match game %', p_game_id;
  end if;

  insert into player_global_stats (user_id)
    select unnest(p_placements) on conflict do nothing;
  insert into player_group_stats (group_id, user_id)
    select g.group_id, unnest(p_placements) on conflict do nothing;
  -- lock everyone's global row in a fixed order (no deadlocks between concurrent games)
  perform 1 from player_global_stats where user_id = any (p_placements) order by user_id for update;

  with placed as (
    select u.user_id, u.ord::int as place
      from unnest(p_placements) with ordinality as u(user_id, ord)
  ),
  pre as (
    select p.user_id, p.place, s.rating
      from placed p join player_global_stats s on s.user_id = p.user_id
  ),
  delta as (
    select a.user_id, a.place, a.rating as old_rating,
           round((p_k / (v_n - 1) * sum(
             (case when a.place < b.place then 1.0 else 0.0 end)
             - 1.0 / (1.0 + power(10.0, (b.rating - a.rating) / 400.0))
           ))::numeric)::int as change
      from pre a join pre b on b.user_id <> a.user_id
     group by a.user_id, a.place, a.rating
  ),
  league as (
    select gp.user_id, st.wins, st.draws, st.losses, st.clashes_won, st.clashes_lost
      from standings st join game_players gp on gp.id = st.game_player_id
     where st.game_id = p_game_id
  ),
  fin as (
    select gp.user_id,
           case when m.p1_id = gp.id then m.p1_score else m.p2_score end as cw,
           case when m.p1_id = gp.id then m.p2_score else m.p1_score end as cl
      from matches m join game_players gp on gp.id in (m.p1_id, m.p2_id)
     where m.game_id = p_game_id and m.stage = 'FINAL'
  ),
  line as (
    select d.user_id, d.place, d.old_rating, d.change,
           coalesce(l.wins, 0) as w, coalesce(l.draws, 0) as dr, coalesce(l.losses, 0) as lo,
           coalesce(l.clashes_won, 0)  + coalesce(f.cw, 0) as cw,
           coalesce(l.clashes_lost, 0) + coalesce(f.cl, 0) as cl
      from delta d
      left join league l on l.user_id = d.user_id
      left join fin f    on f.user_id = d.user_id
  ),
  upd_global as (
    update player_global_stats s set
      rating = s.rating + x.change,
      tournaments_played = s.tournaments_played + 1,
      championships_won  = s.championships_won  + (x.place = 1)::int,
      runner_up_finishes = s.runner_up_finishes + (x.place = 2)::int,
      matches_won = s.matches_won + x.w, matches_drawn = s.matches_drawn + x.dr,
      matches_lost = s.matches_lost + x.lo,
      clashes_won = s.clashes_won + x.cw, clashes_lost = s.clashes_lost + x.cl,
      updated_at = now()
     from line x where s.user_id = x.user_id
    returning s.user_id
  ),
  upd_group as (
    update player_group_stats s set
      tournaments_played = s.tournaments_played + 1,
      championships_won  = s.championships_won  + (x.place = 1)::int,
      runner_up_finishes = s.runner_up_finishes + (x.place = 2)::int,
      matches_won = s.matches_won + x.w, matches_drawn = s.matches_drawn + x.dr,
      matches_lost = s.matches_lost + x.lo,
      clashes_won = s.clashes_won + x.cw, clashes_lost = s.clashes_lost + x.cl,
      updated_at = now()
     from line x where s.group_id = g.group_id and s.user_id = x.user_id
    returning s.user_id
  )
  insert into rating_history (user_id, game_id, old_rating, new_rating, rating_change)
    select x.user_id, p_game_id, x.old_rating, x.old_rating + x.change, x.change from line x;

  update games set leaderboard_processed = true where id = p_game_id;
  return jsonb_build_object('status', 'OK');
end $$;

-- ───────────── reading the boards (one round trip, indexed, paginated) ─────────────
-- Ties: players with identical (rating, championships, runner-ups) share a rank
-- (1,2,2,4 style). Inside a tie the order is by user id so pages never shuffle.
create or replace function global_leaderboard(
  p_user_id bigint, p_limit int default 10, p_offset int default 0
) returns jsonb language sql stable as $$
  with page as (
    select s.* from player_global_stats s
     order by s.rating desc, s.championships_won desc, s.runner_up_finishes desc, s.user_id
     limit p_limit offset p_offset
  )
  select jsonb_build_object(
    'total', (select count(*) from player_global_stats),
    'rows', coalesce((
      select jsonb_agg(jsonb_build_object(
        'rank', 1 + (select count(*) from player_global_stats t
                      where (t.rating, t.championships_won, t.runner_up_finishes)
                          > (p.rating, p.championships_won, p.runner_up_finishes)),
        'user_id', p.user_id,
        'name', coalesce(nullif(u.first_name, ''), u.username, 'Player'),
        'rating', p.rating,
        'championships_won', p.championships_won,
        'tournaments_played', p.tournaments_played)
        order by p.rating desc, p.championships_won desc, p.runner_up_finishes desc, p.user_id)
      from page p join users u on u.id = p.user_id), '[]'::jsonb),
    'me', (
      select jsonb_build_object(
        'rank', 1 + (select count(*) from player_global_stats t
                      where (t.rating, t.championships_won, t.runner_up_finishes)
                          > (m.rating, m.championships_won, m.runner_up_finishes)),
        'rating', m.rating, 'tournaments_played', m.tournaments_played)
      from player_global_stats m where m.user_id = p_user_id)
  );
$$;

-- p_criteria: 'champs' (default) | 'wins' | 'winrate'
--   champs : championships → runner-ups → league matches won
--   wins   : league matches won → clashes won → championships
--   winrate: win % of league matches (needs >= p_min_matches played) → matches played → championships
create or replace function group_leaderboard(
  p_group_id bigint, p_criteria text, p_user_id bigint,
  p_limit int default 10, p_offset int default 0, p_min_matches int default 5
) returns jsonb language sql stable as $$
  with keyed as (
    select s.user_id, s.tournaments_played, s.championships_won, s.runner_up_finishes,
           s.matches_won, s.matches_lost, s.matches_drawn, s.clashes_won, s.clashes_lost,
           (s.matches_won + s.matches_lost + s.matches_drawn) as matches_played,
           case when (s.matches_won + s.matches_lost + s.matches_drawn) > 0
                then round(100.0 * s.matches_won / (s.matches_won + s.matches_lost + s.matches_drawn), 1)
                else 0 end as win_rate
      from player_group_stats s
     where s.group_id = p_group_id
  ),
  scored as (
    select k.*,
      (case p_criteria when 'wins' then k.matches_won when 'winrate' then k.win_rate
                       else k.championships_won end)::numeric as k1,
      (case p_criteria when 'wins' then k.clashes_won when 'winrate' then k.matches_played
                       else k.runner_up_finishes end)::numeric as k2,
      (case p_criteria when 'wins' then k.championships_won when 'winrate' then k.championships_won
                       else k.matches_won end)::numeric as k3
      from keyed k
     where p_criteria <> 'winrate' or k.matches_played >= p_min_matches
  ),
  ranked as (
    select c.*,
           rank()       over (order by c.k1 desc, c.k2 desc, c.k3 desc)               as rnk,
           row_number() over (order by c.k1 desc, c.k2 desc, c.k3 desc, c.user_id)    as rn
      from scored c
  )
  select jsonb_build_object(
    'title', (select title from groups where id = p_group_id),
    'total', (select count(*) from ranked),
    'rows', coalesce((
      select jsonb_agg(jsonb_build_object(
        'rank', r.rnk, 'user_id', r.user_id,
        'name', coalesce(nullif(u.first_name, ''), u.username, 'Player'),
        'tournaments_played', r.tournaments_played, 'championships_won', r.championships_won,
        'runner_up_finishes', r.runner_up_finishes, 'matches_won', r.matches_won,
        'matches_lost', r.matches_lost, 'matches_drawn', r.matches_drawn,
        'matches_played', r.matches_played, 'win_rate', r.win_rate) order by r.rn)
      from ranked r join users u on u.id = r.user_id
     where r.rn > p_offset and r.rn <= p_offset + p_limit), '[]'::jsonb),
    'me',       (select to_jsonb(r) || jsonb_build_object('rank', r.rnk) from ranked r where r.user_id = p_user_id),
    'me_stats', (select to_jsonb(k) from keyed k  where k.user_id = p_user_id)
  );
$$;

-- ───────────── security (same model as the rest of the schema) ─────────────
alter table player_global_stats enable row level security;
alter table player_group_stats  enable row level security;
alter table rating_history      enable row level security;

revoke all on function record_game_results(bigint, bigint[], numeric)               from public, anon, authenticated;
revoke all on function global_leaderboard(bigint, int, int)                         from public, anon, authenticated;
revoke all on function group_leaderboard(bigint, text, bigint, int, int, int)       from public, anon, authenticated;
grant execute on function record_game_results(bigint, bigint[], numeric)            to service_role;
grant execute on function global_leaderboard(bigint, int, int)                      to service_role;
grant execute on function group_leaderboard(bigint, text, bigint, int, int, int)    to service_role;
