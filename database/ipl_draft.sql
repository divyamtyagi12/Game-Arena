-- ════════════════════════════════════════════════════════════════════
--  IPL DRAFT  — run AFTER schema.sql and number_wars.sql.   Idempotent / additive only.
--  Supabase → SQL Editor → paste → Run.
--
--  * Creates ONLY new objects (ipl_* tables/functions + the `ipl_private` schema).
--  * Existing tables, functions and rows are never dropped or altered.
--  * Adds two NEW insert-triggers (on games, nw_matches) so the "one unfinished game per group"
--    rule keeps holding across all three games. Existing triggers are left untouched.
--  * Every table has RLS enabled with NO policies; every function is revoked from
--    public/anon/authenticated and granted to service_role only (same model as the other games).
--  * Hidden player ratings live in schema `ipl_private`, which PostgREST does not expose; the only
--    way to read them is the service-role-only function ipl_engine_squads().
-- ════════════════════════════════════════════════════════════════════

create schema if not exists ipl_private;
revoke all on schema ipl_private from public, anon, authenticated;

-- ───────────────────────── player database ─────────────────────────
create table if not exists ipl_player_stat_sources (
  id            bigint generated always as identity primary key,
  name          text   not null,
  data_version  text   not null default '',
  url           text,
  license       text,
  notes         text,
  record_count  int,
  fetched_at    timestamptz not null default now(),
  unique (name, data_version)
);

create table if not exists ipl_players (
  id               uuid primary key default gen_random_uuid(),
  slug             text   not null unique,
  full_name        text   not null,
  display_name     text   not null,
  name_key         text   not null,                    -- normalised name (dedupe helper)
  cricsheet_id     text   unique,                      -- stable Cricsheet "identifier" when known
  role             text   not null check (role in ('BAT','BOWL','AR','WK')),
  batting_style    text   check (batting_style in ('RHB','LHB')),
  bowling_type     text   not null default 'NONE' check (bowling_type in ('PACE','SPIN','NONE')),
  bowling_style    text,                               -- free text, e.g. 'Right-arm fast-medium'
  nationality      text   not null default '',
  first_ipl_season smallint,
  last_ipl_season  smallint,
  is_eligible      boolean not null default true,      -- false = hidden from the draft pool
  ipl_verified     boolean not null default false,     -- true once an IPL appearance is confirmed from data
  data_quality     text   not null default 'SEED' check (data_quality in ('SEED','CRICSHEET')),
  source_id        bigint references ipl_player_stat_sources(id),
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create unique index if not exists uq_ipl_players_display on ipl_players (lower(display_name));
create index if not exists idx_ipl_players_name_key on ipl_players (name_key);
create index if not exists idx_ipl_players_eligible on ipl_players (is_eligible) where is_eligible;

-- Public, real career numbers (NULL for SEED players: we never invent statistics).
create table if not exists ipl_player_career_stats (
  player_id      uuid primary key references ipl_players(id) on delete cascade,
  matches        int, innings_bat int, runs int, balls_faced int, outs int, fours int, sixes int,
  highest_score  int, fifties int, hundreds int,
  innings_bowl   int, balls_bowled int, runs_conceded int, wickets int, best_bowling text,
  catches        int, stumpings int, run_outs int,
  updated_at     timestamptz not null default now()
);

create table if not exists ipl_player_seasons (
  player_id     uuid not null references ipl_players(id) on delete cascade,
  season        smallint not null,
  matches       int, runs int, balls_faced int, outs int, fours int, sixes int,
  balls_bowled  int, runs_conceded int, wickets int,
  primary key (player_id, season)
);

-- HIDDEN ratings (never exposed through PostgREST: schema ipl_private is not an exposed schema)
create table if not exists ipl_private.ipl_player_private_ratings (
  player_id             uuid primary key references public.ipl_players(id) on delete cascade,
  batting_rating        smallint not null check (batting_rating        between 0 and 100),
  bowling_rating        smallint not null check (bowling_rating        between 0 and 100),
  fielding_rating       smallint not null check (fielding_rating       between 0 and 100),
  wicketkeeping_rating  smallint not null check (wicketkeeping_rating  between 0 and 100),
  batting_consistency   smallint not null check (batting_consistency   between 0 and 100),
  bowling_consistency   smallint not null check (bowling_consistency   between 0 and 100),
  power_hitting         smallint not null check (power_hitting         between 0 and 100),
  strike_rotation       smallint not null check (strike_rotation       between 0 and 100),
  death_overs_batting   smallint not null check (death_overs_batting   between 0 and 100),
  death_overs_bowling   smallint not null check (death_overs_bowling   between 0 and 100),
  spin_effectiveness    smallint not null check (spin_effectiveness    between 0 and 100),
  pace_effectiveness    smallint not null check (pace_effectiveness    between 0 and 100),
  overall_rating        smallint not null check (overall_rating        between 0 and 100),
  rating_source         text not null check (rating_source in ('CRICSHEET_STATS','EDITORIAL_TIER')),
  method_version        text not null,
  computed_at           timestamptz not null default now()
);

-- ───────────────────────── tournaments ─────────────────────────
create table if not exists ipl_tournaments (
  id                   bigint generated always as identity primary key,
  group_id             bigint not null references groups(id),
  host_id              bigint not null references users(id),
  state                text   not null default 'LOBBY' check (state in (
                         'LOBBY','DRAFTING','SYSTEM_TEAM_GENERATION','FIXTURE_GENERATION',
                         'LEAGUE_RUNNING','LEAGUE_COMPLETED','PLAYOFF_ELIMINATOR',
                         'PLAYOFF_QUALIFIER_1','PLAYOFF_QUALIFIER_2','PLAYOFF_FINAL',
                         'COMPLETED','CANCELLED','FAILED_RECOVERABLE')),
  resume_state         text,                               -- where FAILED_RECOVERABLE returns to
  min_humans           smallint not null default 2 check (min_humans between 2 and 8),
  max_humans           smallint not null default 8 check (max_humans between 2 and 8),
  draft_seconds        int      not null default 45 check (draft_seconds between 5 and 600),
  lobby_message_id     bigint,
  dashboard_message_id bigint,
  summary_message_id   bigint,
  last_posted_matchday int      not null default 0,
  champion_team_id     bigint,
  runner_up_team_id    bigint,
  system_announced     boolean  not null default false,
  final_announced      boolean  not null default false,
  leaderboard_processed boolean not null default false,
  lease_owner          text,
  lease_until          timestamptz,
  failure_reason       text,
  created_at           timestamptz not null default now(),
  draft_started_at     timestamptz,
  league_started_at    timestamptz,
  completed_at         timestamptz,
  check (min_humans <= max_humans)
);
create index if not exists idx_ipl_tournaments_state on ipl_tournaments (state);
create index if not exists idx_ipl_tournaments_group on ipl_tournaments (group_id, id desc);
-- ONE unfinished IPL tournament per group (database-level duplicate protection)
create unique index if not exists uq_ipl_one_active_per_group
  on ipl_tournaments (group_id) where state not in ('COMPLETED','CANCELLED');

create table if not exists ipl_tournament_teams (
  id             bigint generated always as identity primary key,
  tournament_id  bigint not null references ipl_tournaments(id) on delete cascade,
  team_no        smallint,
  kind           text   not null check (kind in ('HUMAN','SYSTEM')),
  user_id        bigint references users(id),
  franchise_code text,
  name           text   not null,
  short_name     text   not null,
  owner_name     text,
  squad_complete boolean not null default false,
  auto_draft     boolean not null default false,
  timeouts       smallint not null default 0,
  joined_at      timestamptz not null default now(),
  check ((kind = 'HUMAN'  and user_id is not null and franchise_code is null)
      or (kind = 'SYSTEM' and user_id is null     and franchise_code is not null))
);
create unique index if not exists uq_ipl_team_user    on ipl_tournament_teams (tournament_id, user_id) where user_id is not null;
create unique index if not exists uq_ipl_team_no      on ipl_tournament_teams (tournament_id, team_no) where team_no is not null;
create unique index if not exists uq_ipl_team_franch  on ipl_tournament_teams (tournament_id, franchise_code) where franchise_code is not null;
create unique index if not exists uq_ipl_team_name    on ipl_tournament_teams (tournament_id, lower(name));
create index if not exists idx_ipl_team_user on ipl_tournament_teams (user_id, id desc) where user_id is not null;

do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'fk_ipl_champion_team') then
    alter table ipl_tournaments add constraint fk_ipl_champion_team
      foreign key (champion_team_id) references ipl_tournament_teams(id);
  end if;
  if not exists (select 1 from pg_constraint where conname = 'fk_ipl_runner_up_team') then
    alter table ipl_tournaments add constraint fk_ipl_runner_up_team
      foreign key (runner_up_team_id) references ipl_tournament_teams(id);
  end if;
end $$;

-- the tournament-specific available player pool (+ private rating snapshot, taken at draft start)
create table if not exists ipl_tournament_pool (
  tournament_id bigint not null references ipl_tournaments(id) on delete cascade,
  player_id     uuid   not null references ipl_players(id),
  primary key (tournament_id, player_id)
);

create table if not exists ipl_private.ipl_tournament_ratings (
  tournament_id         bigint not null references public.ipl_tournaments(id) on delete cascade,
  player_id             uuid   not null references public.ipl_players(id),
  batting_rating        smallint not null, bowling_rating        smallint not null,
  fielding_rating       smallint not null, wicketkeeping_rating  smallint not null,
  batting_consistency   smallint not null, bowling_consistency   smallint not null,
  power_hitting         smallint not null, strike_rotation       smallint not null,
  death_overs_batting   smallint not null, death_overs_bowling   smallint not null,
  spin_effectiveness    smallint not null, pace_effectiveness    smallint not null,
  overall_rating        smallint not null,
  primary key (tournament_id, player_id)
);

create table if not exists ipl_team_rosters (
  id            bigint generated always as identity primary key,
  tournament_id bigint not null,
  team_id       bigint not null references ipl_tournament_teams(id) on delete cascade,
  player_id     uuid   not null,
  slot          smallint not null check (slot between 1 and 11),
  source        text   not null check (source in ('DRAFT','SYSTEM')),
  picked_at     timestamptz not null default now(),
  unique (tournament_id, player_id),                      -- a cricketer belongs to ONE team per tournament
  unique (team_id, slot),
  foreign key (tournament_id, player_id) references ipl_tournament_pool (tournament_id, player_id)
);
create index if not exists idx_ipl_roster_team on ipl_team_rosters (team_id);

-- batting-order columns live in the base file too (they used to arrive only with migration_ipl_v2.sql, so a fresh
-- deploy of this file alone failed on ipl_engine_squads and the Python layer silently fell back to in-memory state)
alter table ipl_team_rosters      add column if not exists batting_order smallint check (batting_order between 1 and 11);
alter table ipl_tournament_teams  add column if not exists batting_order_confirmed boolean not null default false;
alter table ipl_tournament_teams  add column if not exists batting_order_deadline_at timestamptz;
alter table ipl_tournament_teams  add column if not exists batting_order_message_id bigint;
alter table ipl_tournament_teams  add column if not exists batting_order_prompted_at timestamptz;
alter table ipl_tournament_teams  add column if not exists squad_summary_sent boolean not null default false;

-- ───────────────────────── drafting ─────────────────────────
create table if not exists ipl_draft_offers (
  id               bigint generated always as identity primary key,
  tournament_id    bigint not null references ipl_tournaments(id) on delete cascade,
  team_id          bigint not null references ipl_tournament_teams(id) on delete cascade,
  round_no         smallint not null check (round_no between 1 and 11),
  status           text   not null default 'OPEN' check (status in ('OPEN','PICKED','AUTO_PICKED','CANCELLED')),
  deadline_at      timestamptz not null,
  dm_message_id    bigint,
  picked_player_id uuid,
  created_at       timestamptz not null default now(),
  resolved_at      timestamptz,
  unique (team_id, round_no)                              -- a round can never be opened twice
);
create unique index if not exists uq_ipl_one_open_offer on ipl_draft_offers (team_id) where status = 'OPEN';
create index if not exists idx_ipl_offers_open on ipl_draft_offers (deadline_at) where status = 'OPEN';

create table if not exists ipl_draft_offer_candidates (
  offer_id  bigint   not null references ipl_draft_offers(id) on delete cascade,
  position  smallint not null check (position between 1 and 11),
  player_id uuid     not null references ipl_players(id),
  primary key (offer_id, position),
  unique (offer_id, player_id)
);

create table if not exists ipl_draft_picks (
  id            bigint generated always as identity primary key,
  tournament_id bigint not null,
  team_id       bigint not null references ipl_tournament_teams(id) on delete cascade,
  round_no      smallint not null check (round_no between 1 and 11),
  player_id     uuid   not null,
  offer_id      bigint not null unique references ipl_draft_offers(id) on delete cascade,
  auto_picked   boolean not null default false,
  reason        text   not null default 'USER' check (reason in ('USER','TIMEOUT','AUTO','SYSTEM')),
  picked_at     timestamptz not null default now(),
  unique (team_id, round_no),                             -- one pick per round (no double advance)
  unique (tournament_id, player_id),                      -- one claim per cricketer
  foreign key (tournament_id, player_id) references ipl_tournament_pool (tournament_id, player_id)
);

-- Exclusive reservations: while a manager is choosing, those 11 cricketers cannot be offered elsewhere.
create table if not exists ipl_player_reservations (
  id            bigint generated always as identity primary key,
  tournament_id bigint not null,
  player_id     uuid   not null,
  team_id       bigint not null references ipl_tournament_teams(id) on delete cascade,
  offer_id      bigint not null references ipl_draft_offers(id) on delete cascade,
  reserved_at   timestamptz not null default now(),
  expires_at    timestamptz not null,
  unique (tournament_id, player_id),                      -- DB-enforced exclusivity
  foreign key (tournament_id, player_id) references ipl_tournament_pool (tournament_id, player_id)
);
create index if not exists idx_ipl_res_offer on ipl_player_reservations (offer_id);

-- ───────────────────────── fixtures & matches ─────────────────────────
create table if not exists ipl_fixtures (
  id             bigint generated always as identity primary key,
  tournament_id  bigint not null references ipl_tournaments(id) on delete cascade,
  stage          text   not null check (stage in ('LEAGUE','ELIMINATOR','QUALIFIER_1','QUALIFIER_2','FINAL')),
  match_no       int    not null,
  matchday       int,
  leg            smallint check (leg in (1, 2)),
  home_team_id   bigint not null references ipl_tournament_teams(id),
  away_team_id   bigint not null references ipl_tournament_teams(id),
  status         text   not null default 'SCHEDULED' check (status in ('SCHEDULED','CLAIMED','COMPLETED')),
  claimed_by     text,
  claimed_at     timestamptz,
  completed_at   timestamptz,
  check (home_team_id <> away_team_id),
  unique (tournament_id, match_no),
  unique (tournament_id, stage, home_team_id, away_team_id)   -- no duplicate fixture
);
create unique index if not exists uq_ipl_playoff_stage on ipl_fixtures (tournament_id, stage) where stage <> 'LEAGUE';
create index if not exists idx_ipl_fixtures_todo on ipl_fixtures (tournament_id, match_no) where status <> 'COMPLETED';

create table if not exists ipl_matches (
  id             bigint generated always as identity primary key,
  fixture_id     bigint not null unique references ipl_fixtures(id),   -- ONE result per fixture
  tournament_id  bigint not null references ipl_tournaments(id) on delete cascade,
  stage          text   not null,
  match_no       int    not null,
  team1_id       bigint not null references ipl_tournament_teams(id),   -- batted first
  team2_id       bigint not null references ipl_tournament_teams(id),
  toss_winner_id bigint references ipl_tournament_teams(id),
  toss_decision  text   check (toss_decision in ('BAT','FIELD')),
  winner_team_id bigint references ipl_tournament_teams(id),
  result_type    text   not null check (result_type in ('RUNS','WICKETS','TIE_SUPER_OVER','NO_RESULT')),
  margin         int,
  super_overs    smallint not null default 0,
  pom_player_id  uuid references ipl_players(id),
  best_bowler_id uuid references ipl_players(id),
  summary        text,
  rng_seed       text,
  created_at     timestamptz not null default now(),
  check (winner_team_id is null or winner_team_id in (team1_id, team2_id))
);
create index if not exists idx_ipl_matches_t on ipl_matches (tournament_id, match_no);

create table if not exists ipl_innings (
  id              bigint generated always as identity primary key,
  match_id        bigint not null references ipl_matches(id) on delete cascade,
  innings_no      smallint not null,
  is_super_over   boolean  not null default false,
  batting_team_id bigint not null references ipl_tournament_teams(id),
  bowling_team_id bigint not null references ipl_tournament_teams(id),
  runs            int  not null check (runs >= 0),
  wickets         smallint not null check (wickets between 0 and 10),
  legal_balls     int  not null check (legal_balls >= 0),
  extras          jsonb not null default '{}'::jsonb,
  fall_of_wickets jsonb not null default '[]'::jsonb,
  target          int,
  all_out         boolean not null default false,
  unique (match_id, innings_no)
);

create table if not exists ipl_batting_scorecards (
  innings_id  bigint not null references ipl_innings(id) on delete cascade,
  player_id   uuid   not null references ipl_players(id),
  batting_pos smallint not null,
  did_bat     boolean  not null default true,
  runs        int not null default 0,
  balls       int not null default 0,
  fours       int not null default 0,
  sixes       int not null default 0,
  is_out      boolean not null default false,
  dismissal   text,
  bowler_id   uuid,
  fielder_id  uuid,
  primary key (innings_id, player_id)
);
create index if not exists idx_ipl_bat_player on ipl_batting_scorecards (player_id);

create table if not exists ipl_bowling_scorecards (
  innings_id  bigint not null references ipl_innings(id) on delete cascade,
  player_id   uuid   not null references ipl_players(id),
  legal_balls int not null default 0,
  runs        int not null default 0,
  wickets     int not null default 0,
  maidens     int not null default 0,
  wides       int not null default 0,
  noballs     int not null default 0,
  dots        int not null default 0,
  primary key (innings_id, player_id)
);
create index if not exists idx_ipl_bowl_player on ipl_bowling_scorecards (player_id);

create table if not exists ipl_ball_events (
  id            bigint generated always as identity primary key,
  innings_id    bigint not null references ipl_innings(id) on delete cascade,
  seq           int      not null,
  over_no       smallint not null,
  ball_no       smallint not null,
  striker_id    uuid, non_striker_id uuid, bowler_id uuid,
  runs_bat      smallint not null default 0,
  extras_type   text,
  extras_runs   smallint not null default 0,
  wicket_kind   text,
  player_out_id uuid,
  fielder_id    uuid,
  legal         boolean not null default true,
  unique (innings_id, seq)
);

-- ───────────────────────── standings / bracket / awards / leaderboards ─────────────────────────
create table if not exists ipl_standings (
  tournament_id bigint not null references ipl_tournaments(id) on delete cascade,
  team_id       bigint not null references ipl_tournament_teams(id) on delete cascade,
  played        int not null default 0,
  won           int not null default 0,
  lost          int not null default 0,
  no_result     int not null default 0,
  points        int not null default 0,
  runs_for      int not null default 0,
  balls_faced   int not null default 0,        -- an all-out innings counts as the full 120 balls
  runs_against  int not null default 0,
  balls_bowled  int not null default 0,        -- (bowled-out opposition: full 120 balls)
  nrr           numeric(8,3) generated always as (
                  case when balls_faced > 0 and balls_bowled > 0
                       then round(runs_for * 6.0 / balls_faced - runs_against * 6.0 / balls_bowled, 3)
                       else 0 end) stored,
  rank          smallint,
  updated_at    timestamptz not null default now(),
  primary key (tournament_id, team_id)
);

create table if not exists ipl_playoff_matches (
  tournament_id bigint not null references ipl_tournaments(id) on delete cascade,
  stage         text   not null check (stage in ('ELIMINATOR','QUALIFIER_1','QUALIFIER_2','FINAL')),
  slot          smallint not null,
  home_source   text   not null,
  away_source   text   not null,
  fixture_id    bigint references ipl_fixtures(id),
  primary key (tournament_id, stage)
);

create table if not exists ipl_tournament_awards (
  tournament_id bigint not null references ipl_tournaments(id) on delete cascade,
  award         text   not null,
  player_id     uuid references ipl_players(id),
  team_id       bigint references ipl_tournament_teams(id),
  value_text    text,
  value_num     numeric,
  primary key (tournament_id, award)
);

-- one row per (finished tournament, human team): the single source for the leaderboards
create table if not exists ipl_tournament_results (
  tournament_id    bigint not null references ipl_tournaments(id) on delete cascade,
  team_id          bigint not null references ipl_tournament_teams(id) on delete cascade,
  user_id          bigint not null references users(id),
  group_id         bigint not null references groups(id),
  league_rank      smallint,
  champion         boolean not null default false,
  runner_up        boolean not null default false,
  playoff          boolean not null default false,
  league_won       int not null default 0,
  league_lost      int not null default 0,
  league_played    int not null default 0,
  runs_scored      int not null default 0,
  wickets_taken    int not null default 0,
  recorded_at      timestamptz not null default now(),
  primary key (tournament_id, team_id)
);
create index if not exists idx_ipl_results_user on ipl_tournament_results (user_id);
create index if not exists idx_ipl_results_group on ipl_tournament_results (group_id, user_id);

create table if not exists ipl_user_stats (
  user_id             bigint primary key references users(id),
  tournaments_played  int not null default 0,
  championships       int not null default 0,
  runner_ups          int not null default 0,
  playoff_appearances int not null default 0,
  league_wins         int not null default 0,
  league_losses       int not null default 0,
  league_played       int not null default 0,
  runs_scored         int not null default 0,
  wickets_taken       int not null default 0,
  updated_at          timestamptz not null default now()
);
create index if not exists idx_ipl_user_rank on ipl_user_stats (championships desc, runner_ups desc, league_wins desc);

create table if not exists ipl_group_stats (
  group_id            bigint not null references groups(id),
  user_id             bigint not null references users(id),
  tournaments_played  int not null default 0,
  championships       int not null default 0,
  runner_ups          int not null default 0,
  playoff_appearances int not null default 0,
  league_wins         int not null default 0,
  league_losses       int not null default 0,
  league_played       int not null default 0,
  runs_scored         int not null default 0,
  wickets_taken       int not null default 0,
  updated_at          timestamptz not null default now(),
  primary key (group_id, user_id)
);
create index if not exists idx_ipl_group_rank on ipl_group_stats (group_id, championships desc, runner_ups desc, league_wins desc);
create index if not exists idx_ipl_group_user on ipl_group_stats (user_id, updated_at desc);

-- ───────────────────────── integrity triggers ─────────────────────────
-- 1) explicit state machine: invalid transitions are rejected by the database itself
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

create or replace function ipl_trg_state_machine() returns trigger language plpgsql as $$
begin
  if new.state is distinct from old.state then
    if not ipl_valid_transition(old.state, new.state) then
      raise exception 'invalid IPL state transition % -> %', old.state, new.state using errcode = 'P0001';
    end if;
    if new.state = 'FAILED_RECOVERABLE' then new.resume_state := old.state; end if;
    if old.state = 'FAILED_RECOVERABLE' then
      if new.state <> 'CANCELLED' and new.state is distinct from old.resume_state then
        raise exception 'must resume to % (not %)', old.resume_state, new.state using errcode = 'P0001';
      end if;
      new.resume_state := null; new.failure_reason := null;
    end if;
  end if;
  return new;
end $$;
drop trigger if exists trg_ipl_state_machine on ipl_tournaments;
create trigger trg_ipl_state_machine before update of state on ipl_tournaments
  for each row execute function ipl_trg_state_machine();

-- 2) a squad is "complete" only with exactly 11 rostered players; roster/slot integrity
create or replace function ipl_trg_squad_complete() returns trigger language plpgsql as $$
begin
  if new.squad_complete and not coalesce(old.squad_complete, false) then
    if (select count(*) from ipl_team_rosters where team_id = new.id) <> 11 then
      raise exception 'squad must contain exactly 11 players' using errcode = 'P0001';
    end if;
  end if;
  return new;
end $$;
drop trigger if exists trg_ipl_squad_complete on ipl_tournament_teams;
create trigger trg_ipl_squad_complete before update of squad_complete on ipl_tournament_teams
  for each row execute function ipl_trg_squad_complete();

-- 3) hard cap: never more than 8 human teams in one tournament
create or replace function ipl_trg_human_cap() returns trigger language plpgsql as $$
begin
  if new.kind = 'HUMAN' and
     (select count(*) from ipl_tournament_teams where tournament_id = new.tournament_id and kind = 'HUMAN') >= 8 then
    raise exception 'a tournament has at most 8 human teams' using errcode = 'P0001';
  end if;
  return new;
end $$;
drop trigger if exists trg_ipl_human_cap on ipl_tournament_teams;
create trigger trg_ipl_human_cap before insert on ipl_tournament_teams
  for each row execute function ipl_trg_human_cap();

-- 4) one unfinished game of ANY kind per group. The two older games already guard themselves
--    against each other; these NEW triggers add IPL to the cross-check (same advisory lock key).
create or replace function ipl_guard_new_tournament() returns trigger language plpgsql as $$
begin
  perform pg_advisory_xact_lock(new.group_id);
  if exists (select 1 from games where group_id = new.group_id
              and status in ('LOBBY','DRAFTING','LEAGUE','FINAL'))
     or exists (select 1 from nw_matches where group_id = new.group_id
              and status in ('LOBBY','ACTIVE')) then
    raise exception 'group already has an active game' using errcode = '23505';
  end if;
  return new;
end $$;
drop trigger if exists trg_ipl_guard on ipl_tournaments;
create trigger trg_ipl_guard before insert on ipl_tournaments
  for each row execute function ipl_guard_new_tournament();

create or replace function ipl_guard_other_games() returns trigger language plpgsql as $$
begin
  perform pg_advisory_xact_lock(new.group_id);
  if exists (select 1 from ipl_tournaments where group_id = new.group_id
              and state not in ('COMPLETED','CANCELLED')) then
    raise exception 'group already has an active game' using errcode = '23505';
  end if;
  return new;
end $$;
drop trigger if exists trg_games_guard_ipl on games;
create trigger trg_games_guard_ipl before insert on games
  for each row execute function ipl_guard_other_games();
drop trigger if exists trg_nw_guard_ipl on nw_matches;
create trigger trg_nw_guard_ipl before insert on nw_matches
  for each row execute function ipl_guard_other_games();

-- ════════════════════════════════════════════════════════════════════
--  FUNCTIONS (all called via RPC with the service-role key)
-- ════════════════════════════════════════════════════════════════════

-- ───────────── json helpers ─────────────
create or replace function ipl_player_json(p ipl_players) returns jsonb
language sql stable as $$
  select jsonb_build_object('id', p.id, 'name', p.display_name, 'role', p.role,
           'nationality', p.nationality, 'batting_style', p.batting_style,
           'bowling_type', p.bowling_type, 'bowling_style', p.bowling_style)
$$;

create or replace function ipl_team_json(t ipl_tournament_teams) returns jsonb
language sql stable as $$
  select jsonb_build_object('id', t.id, 'tournament_id', t.tournament_id, 'team_no', t.team_no, 'kind', t.kind,
           'user_id', t.user_id, 'franchise_code', t.franchise_code, 'name', t.name,
           'short_name', t.short_name, 'owner_name', t.owner_name, 'squad_complete', t.squad_complete,
           'auto_draft', t.auto_draft, 'timeouts', t.timeouts,
           'squad_summary_sent', coalesce(t.squad_summary_sent, false))
$$;

create or replace function ipl_mark_squad_summary_sent(p_team_id bigint) returns boolean
language plpgsql security definer set search_path = public, ipl_private as $$
begin
  update ipl_tournament_teams set squad_summary_sent = true where id = p_team_id;
  return found;
end $$;

create or replace function ipl_tournament_json(t ipl_tournaments) returns jsonb
language sql stable as $$
  select to_jsonb(t) - 'lease_owner' - 'lease_until'
$$;

-- ───────────── lobby ─────────────
create or replace function ipl_create_tournament(p_group_id bigint, p_host_id bigint, p_min int, p_max int, p_draft_seconds int)
returns jsonb language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype;
begin
  insert into ipl_tournaments (group_id, host_id, min_humans, max_humans, draft_seconds)
  values (p_group_id, p_host_id, greatest(p_min, 2), least(p_max, 8), p_draft_seconds)
  returning * into t;
  return ipl_tournament_json(t);
exception when unique_violation then
  return null;
end $$;

create or replace function ipl_get_tournament(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select ipl_tournament_json(t) from ipl_tournaments t where t.id = p_tid
$$;

create or replace function ipl_active_for_group(p_group_id bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select ipl_tournament_json(t) from ipl_tournaments t
   where t.group_id = p_group_id and t.state not in ('COMPLETED','CANCELLED') limit 1
$$;

create or replace function ipl_latest_for_group(p_group_id bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select ipl_tournament_json(t) from ipl_tournaments t
   where t.group_id = p_group_id and t.state <> 'CANCELLED' order by t.id desc limit 1
$$;

create or replace function ipl_active_for_user(p_user_id bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select ipl_tournament_json(t) from ipl_tournaments t
    join ipl_tournament_teams tm on tm.tournament_id = t.id and tm.user_id = p_user_id
   where t.state not in ('COMPLETED','CANCELLED')
   order by t.id desc limit 1
$$;

create or replace function ipl_latest_for_user(p_user_id bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select ipl_tournament_json(t) from ipl_tournaments t
    join ipl_tournament_teams tm on tm.tournament_id = t.id and tm.user_id = p_user_id
   where t.state <> 'CANCELLED'
   order by t.id desc limit 1
$$;

create or replace function ipl_by_states(p_states jsonb) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(ipl_tournament_json(t) order by t.id), '[]'::jsonb)
    from ipl_tournaments t where t.state in (select jsonb_array_elements_text(p_states))
$$;

create or replace function ipl_unannounced_completed() returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(ipl_tournament_json(t) order by t.id), '[]'::jsonb)
    from ipl_tournaments t where t.state = 'COMPLETED' and not t.final_announced
$$;

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

create or replace function ipl_lobby_teams(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(ipl_team_json(t) order by t.team_no nulls last, t.joined_at, t.id), '[]'::jsonb)
    from ipl_tournament_teams t where t.tournament_id = p_tid and t.kind = 'HUMAN'
$$;

create or replace function ipl_all_teams(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(ipl_team_json(t) order by t.team_no nulls last, t.id), '[]'::jsonb)
    from ipl_tournament_teams t where t.tournament_id = p_tid
$$;

create or replace function ipl_join(p_tid bigint, p_user_id bigint, p_name text)
returns text language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype; cnt int; base text; nm text; n int := 1;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found or t.state <> 'LOBBY' then return 'CLOSED'; end if;
  if exists (select 1 from ipl_tournament_teams where tournament_id = p_tid and user_id = p_user_id) then
    return 'ALREADY';
  end if;
  select count(*) into cnt from ipl_tournament_teams where tournament_id = p_tid and kind = 'HUMAN';
  if cnt >= t.max_humans then return 'FULL'; end if;
  base := left(btrim(p_name), 18);
  if base = '' then base := 'Player'; end if;
  nm := base || ' XI';
  while exists (select 1 from ipl_tournament_teams where tournament_id = p_tid and lower(name) = lower(nm)) loop
    n := n + 1; nm := base || ' XI ' || n;
  end loop;
  insert into ipl_tournament_teams (tournament_id, kind, user_id, name, short_name, owner_name)
  values (p_tid, 'HUMAN', p_user_id, nm, left(upper(regexp_replace(base, '[^A-Za-z0-9]', '', 'g')), 4) || 'XI', base);
  return 'OK';
end $$;

create or replace function ipl_leave(p_tid bigint, p_user_id bigint)
returns text language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found or t.state <> 'LOBBY' then return 'CLOSED'; end if;
  delete from ipl_tournament_teams where tournament_id = p_tid and user_id = p_user_id;
  if found then return 'OK'; end if;
  return 'NOT_IN';
end $$;

create or replace function ipl_cancel(p_tid bigint) returns boolean
language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found or t.state in ('COMPLETED','CANCELLED') then return false; end if;
  update ipl_draft_offers set status = 'CANCELLED', resolved_at = now() where tournament_id = p_tid and status = 'OPEN';
  delete from ipl_player_reservations where tournament_id = p_tid;
  update ipl_tournaments set state = 'CANCELLED', completed_at = now() where id = p_tid;
  return true;
end $$;

create or replace function ipl_set_failed(p_tid bigint, p_reason text) returns boolean
language plpgsql security definer set search_path = public, ipl_private as $$
begin
  update ipl_tournaments
     set resume_state = state,
         state = 'FAILED_RECOVERABLE',
         failure_reason = left(p_reason, 500)
   where id = p_tid and state not in ('LOBBY','COMPLETED','CANCELLED','FAILED_RECOVERABLE');
  return found;
end $$;

create or replace function ipl_resume(p_tid bigint) returns text
language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found or t.state <> 'FAILED_RECOVERABLE' then return null; end if;
  update ipl_tournaments set state = t.resume_state where id = p_tid;
  return t.resume_state;
end $$;

-- lease: at most one driver per tournament at a time (across processes)
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

-- ───────────── player pool & start of drafting ─────────────
create or replace function ipl_pool_summary() returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select jsonb_build_object(
    'eligible',        (select count(*) from ipl_players p where p.is_eligible),
    'with_ratings',    (select count(*) from ipl_players p join ipl_private.ipl_player_private_ratings r on r.player_id = p.id where p.is_eligible),
    'verified',        (select count(*) from ipl_players p where p.is_eligible and p.ipl_verified),
    'missing_ratings', (select count(*) from ipl_players p where p.is_eligible
                          and not exists (select 1 from ipl_private.ipl_player_private_ratings r where r.player_id = p.id)),
    'by_role',         coalesce((select jsonb_object_agg(role, n) from (
                          select p.role, count(*) n from ipl_players p
                            join ipl_private.ipl_player_private_ratings r on r.player_id = p.id
                           where p.is_eligible group by p.role) x), '{}'::jsonb))
$$;

-- Squad-capacity rule (see docs): the pool must cover every final roster
--   (humans + 8) x 11,  AND the worst moment of simultaneous 11-card reservations
--   (every human on their last round: 10H claimed + 11(H-1) held by others + 11 offered = 21H),
--   plus a safety margin.
create or replace function ipl_required_pool(p_humans int, p_margin int default 20) returns int
language sql immutable as $$ select greatest((p_humans + 5) * 11, 21 * p_humans) + p_margin $$;

create or replace function ipl_start_draft(p_tid bigint, p_margin int default 20)
returns jsonb language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype; h int; have int; need int;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found or t.state <> 'LOBBY' then return jsonb_build_object('status', 'NOT_LOBBY'); end if;
  select count(*) into h from ipl_tournament_teams where tournament_id = p_tid and kind = 'HUMAN';
  if h < t.min_humans then
    return jsonb_build_object('status', 'TOO_FEW', 'have', h, 'need', t.min_humans);
  end if;
  select count(*) into have from ipl_players p
    join ipl_private.ipl_player_private_ratings r on r.player_id = p.id where p.is_eligible;
  need := ipl_required_pool(h, p_margin);
  if have < need then
    return jsonb_build_object('status', 'POOL_TOO_SMALL', 'have', have, 'need', need);
  end if;

  with ordered as (
    select id, row_number() over (order by joined_at, id) rn
      from ipl_tournament_teams where tournament_id = p_tid and kind = 'HUMAN')
  update ipl_tournament_teams tm set team_no = o.rn from ordered o where tm.id = o.id;

  insert into ipl_tournament_pool (tournament_id, player_id)
    select p_tid, p.id from ipl_players p
      join ipl_private.ipl_player_private_ratings r on r.player_id = p.id where p.is_eligible;
  -- frozen copy of the hidden ratings: later data refreshes can't change a running tournament
  insert into ipl_private.ipl_tournament_ratings
    select p_tid, r.player_id, r.batting_rating, r.bowling_rating, r.fielding_rating, r.wicketkeeping_rating,
           r.batting_consistency, r.bowling_consistency, r.power_hitting, r.strike_rotation,
           r.death_overs_batting, r.death_overs_bowling, r.spin_effectiveness, r.pace_effectiveness,
           r.overall_rating
      from ipl_private.ipl_player_private_ratings r
      join ipl_players p on p.id = r.player_id where p.is_eligible;

  update ipl_tournaments set state = 'DRAFTING', draft_started_at = now() where id = p_tid;
  return jsonb_build_object('status', 'OK', 'humans', h, 'pool', have, 'need', need);
end $$;

-- ───────────── draft offers / picks ─────────────
create or replace function ipl_offer_json(p_offer_id bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select jsonb_build_object(
    'offer_id', o.id, 'tournament_id', o.tournament_id, 'team_id', o.team_id, 'round_no', o.round_no,
    'status', o.status, 'deadline_at', o.deadline_at, 'dm_message_id', o.dm_message_id,
    'picked_player_id', o.picked_player_id,
    'candidates', coalesce((select jsonb_agg(jsonb_build_object('pos', c.position, 'player', ipl_player_json(p)) order by c.position)
                              from ipl_draft_offer_candidates c join ipl_players p on p.id = c.player_id
                             where c.offer_id = o.id), '[]'::jsonb))
  from ipl_draft_offers o where o.id = p_offer_id
$$;

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

  ids := '{}'::uuid[];

  -- If keeper is needed, guarantee options for WK (from pool, or catalog fallback)
  if wk_need > 0 then
    if legal_ids is not null then
      select array_agg(id) into deficit_ids from (
        select p.id
          from unnest(legal_ids) lid
          join ipl_players p on p.id = lid
         where p.role = 'WK'
         order by random() limit 4
      ) s;
      if deficit_ids is not null then ids := ids || deficit_ids; end if;
    end if;

    -- Catalog fallback if pool had no legal WK
    if deficit_ids is null or cardinality(deficit_ids) = 0 then
      select array_agg(id) into deficit_ids from (
        select p.id from ipl_players p
         where p.is_eligible and p.role = 'WK'
           and not exists (select 1 from ipl_team_rosters r where r.team_id = p_team_id and r.player_id = p.id)
         order by random() limit 4
      ) s;
      if deficit_ids is not null then
        insert into ipl_tournament_pool (tournament_id, player_id)
          select t.id, x from unnest(deficit_ids) x
        on conflict do nothing;
        ids := ids || deficit_ids;
      end if;
    end if;
  end if;

  -- If bowlers are needed, guarantee options for BOWL
  if b_need > 0 then
    if legal_ids is not null then
      select array_agg(id) into deficit_ids from (
        select p.id
          from unnest(legal_ids) lid
          join ipl_players p on p.id = lid
         where p.role = 'BOWL' and not (p.id = any(ids))
         order by random() limit 4
      ) s;
      if deficit_ids is not null then ids := ids || deficit_ids; end if;
    end if;

    if deficit_ids is null or cardinality(deficit_ids) = 0 then
      select array_agg(id) into deficit_ids from (
        select p.id from ipl_players p
         where p.is_eligible and p.role = 'BOWL' and not (p.id = any(ids))
           and not exists (select 1 from ipl_team_rosters r where r.team_id = p_team_id and r.player_id = p.id)
         order by random() limit 4
      ) s;
      if deficit_ids is not null then
        insert into ipl_tournament_pool (tournament_id, player_id)
          select t.id, x from unnest(deficit_ids) x
        on conflict do nothing;
        ids := ids || deficit_ids;
      end if;
    end if;
  end if;

  -- If all-rounders are needed, guarantee options for AR
  if ar_need > 0 then
    if legal_ids is not null then
      select array_agg(id) into deficit_ids from (
        select p.id
          from unnest(legal_ids) lid
          join ipl_players p on p.id = lid
         where p.role = 'AR' and not (p.id = any(ids))
         order by random() limit 4
      ) s;
      if deficit_ids is not null then ids := ids || deficit_ids; end if;
    end if;

    if deficit_ids is null or cardinality(deficit_ids) = 0 then
      select array_agg(id) into deficit_ids from (
        select p.id from ipl_players p
         where p.is_eligible and p.role = 'AR' and not (p.id = any(ids))
           and not exists (select 1 from ipl_team_rosters r where r.team_id = p_team_id and r.player_id = p.id)
         order by random() limit 4
      ) s;
      if deficit_ids is not null then
        insert into ipl_tournament_pool (tournament_id, player_id)
          select t.id, x from unnest(deficit_ids) x
        on conflict do nothing;
        ids := ids || deficit_ids;
      end if;
    end if;
  end if;

  -- Fill any remaining slots strictly from legal_ids
  if legal_ids is not null and cardinality(ids) < 11 and cardinality(legal_ids) > cardinality(ids) then
    select array_agg(lid) into rem_legal from (
      select lid
        from unnest(legal_ids) lid
       where not (lid = any(ids))
       order by random() limit (11 - cardinality(ids))
    ) s;
    if rem_legal is not null then ids := ids || rem_legal; end if;
  end if;

  -- Role guarantees may add up to 12 cards: de-duplicate and cap at 11 (offer positions are 1..11)
  select coalesce(array_agg(x order by ord), '{}'::uuid[]) into ids from (
    select x, min(ord) as ord from unnest(ids) with ordinality u(x, ord) group by x order by min(ord) limit 11
  ) d;

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

create or replace function ipl_get_offer(p_offer_id bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select ipl_offer_json(p_offer_id) where exists (select 1 from ipl_draft_offers where id = p_offer_id)
$$;

create or replace function ipl_set_offer_message(p_offer_id bigint, p_message_id bigint) returns void
language sql security definer set search_path = public, ipl_private as $$
  update ipl_draft_offers set dm_message_id = p_message_id where id = p_offer_id
$$;

-- p_mode: USER (a manager pressed the button) | TIMEOUT (45s expired) | AUTO (auto-draft mode) | SYSTEM (franchise AI)
create or replace function ipl_pick(p_offer_id bigint, p_pos int, p_user_id bigint, p_mode text default 'USER',
                                    p_auto_after int default 2, p_grace_seconds int default 3)
returns jsonb language plpgsql security definer set search_path = public, ipl_private as $$
declare o ipl_draft_offers%rowtype; tm ipl_tournament_teams%rowtype; t ipl_tournaments%rowtype;
        pid uuid; n int; pl ipl_players%rowtype; humans_left int;
begin
  select * into o from ipl_draft_offers where id = p_offer_id for update;
  if not found then return jsonb_build_object('status', 'STALE'); end if;
  select * into tm from ipl_tournament_teams where id = o.team_id;
  select * into t from ipl_tournaments where id = o.tournament_id;

  if p_mode not in ('USER','TIMEOUT','AUTO','SYSTEM') then return jsonb_build_object('status', 'BAD_MODE'); end if;
  if p_mode = 'USER' and (tm.kind <> 'HUMAN' or tm.user_id is distinct from p_user_id) then
    return jsonb_build_object('status', 'NOT_YOURS');                    -- never trust the button's owner
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

  insert into ipl_draft_picks (tournament_id, team_id, round_no, player_id, offer_id, auto_picked, reason)
  values (o.tournament_id, o.team_id, o.round_no, pid, o.id, p_mode <> 'USER' and p_mode <> 'SYSTEM', p_mode);
  select count(*) into n from ipl_team_rosters where team_id = o.team_id;
  insert into ipl_team_rosters (tournament_id, team_id, player_id, slot, source)
  values (o.tournament_id, o.team_id, pid, n + 1, case when tm.kind = 'HUMAN' then 'DRAFT' else 'SYSTEM' end);
  delete from ipl_player_reservations where offer_id = o.id;             -- release the other 10 immediately
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
  if n + 1 = 11 then update ipl_tournament_teams set squad_complete = true where id = tm.id; end if;

  select * into pl from ipl_players where id = pid;
  select count(*) into humans_left from ipl_tournament_teams
   where tournament_id = o.tournament_id and kind = 'HUMAN' and not squad_complete;
  return jsonb_build_object('status', 'OK', 'player', ipl_player_json(pl), 'round_no', o.round_no,
                            'picks_done', n + 1, 'done', n + 1 = 11, 'team_id', tm.id,
                            'user_id', tm.user_id, 'tournament_id', o.tournament_id,
                            'all_humans_done', humans_left = 0, 'auto', p_mode in ('TIMEOUT','AUTO'));
end $$;

-- offers that need the worker (timed out, or the manager is in auto-draft mode) + team context for the heuristic
create or replace function ipl_due_offers(p_limit int default 50) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(x), '[]'::jsonb) from (
    select ipl_offer_json(o.id) || jsonb_build_object(
             'user_id', tm.user_id, 'auto_draft', tm.auto_draft, 'timed_out', o.deadline_at < now(),
             'roster', coalesce((select jsonb_agg(jsonb_build_object('role', p.role, 'bowling_type', p.bowling_type))
                                   from ipl_team_rosters r join ipl_players p on p.id = r.player_id
                                  where r.team_id = tm.id), '[]'::jsonb)) as x
      from ipl_draft_offers o
      join ipl_tournament_teams tm on tm.id = o.team_id
      join ipl_tournaments t on t.id = o.tournament_id
     where o.status = 'OPEN' and t.state = 'DRAFTING' and tm.kind = 'HUMAN'
       and (o.deadline_at < now() or tm.auto_draft)
     order by o.deadline_at limit p_limit) s
$$;

-- human teams that are not finished and have no open offer (e.g. after a restart)
create or replace function ipl_teams_needing_offer(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(ipl_team_json(tm)), '[]'::jsonb)
    from ipl_tournament_teams tm
   where tm.tournament_id = p_tid and tm.kind = 'HUMAN' and not tm.squad_complete
     -- needs the worker when it has no open offer yet, OR an open offer whose DM was never delivered (crash/restart)
     and not exists (select 1 from ipl_draft_offers o where o.team_id = tm.id and o.status = 'OPEN'
                        and (o.dm_message_id is not null or tm.auto_draft))
$$;

create or replace function ipl_open_offers(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(jsonb_build_object('offer_id', o.id, 'team_id', o.team_id, 'user_id', tm.user_id,
                      'dm_message_id', o.dm_message_id, 'deadline_at', o.deadline_at, 'round_no', o.round_no)), '[]'::jsonb)
    from ipl_draft_offers o join ipl_tournament_teams tm on tm.id = o.team_id
   where o.tournament_id = p_tid and o.status = 'OPEN' and tm.kind = 'HUMAN'
$$;

create or replace function ipl_draft_progress(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(jsonb_build_object('team_id', tm.id, 'name', tm.name, 'user_id', tm.user_id,
           'picks', (select count(*) from ipl_draft_picks k where k.team_id = tm.id),
           'done', tm.squad_complete, 'auto', tm.auto_draft) order by tm.team_no), '[]'::jsonb)
    from ipl_tournament_teams tm where tm.tournament_id = p_tid and tm.kind = 'HUMAN'
$$;

create or replace function ipl_team_roster(p_team_id bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select jsonb_build_object('team', ipl_team_json(tm),
           'players', coalesce((select jsonb_agg(ipl_player_json(p) order by r.slot)
                                  from ipl_team_rosters r join ipl_players p on p.id = r.player_id
                                 where r.team_id = tm.id), '[]'::jsonb))
    from ipl_tournament_teams tm where tm.id = p_team_id
$$;

create or replace function ipl_team_of_user(p_tid bigint, p_user_id bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select ipl_team_json(tm) from ipl_tournament_teams tm where tm.tournament_id = p_tid and tm.user_id = p_user_id
$$;

create or replace function ipl_all_rosters(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(ipl_team_roster(tm.id) order by tm.team_no), '[]'::jsonb)
    from ipl_tournament_teams tm where tm.tournament_id = p_tid
$$;

-- DM undeliverable / manager gone: finish the remaining picks automatically so the draft can never block
create or replace function ipl_set_auto_draft(p_team_id bigint) returns void
language sql security definer set search_path = public, ipl_private as $$
  update ipl_tournament_teams set auto_draft = true where id = p_team_id and kind = 'HUMAN'
$$;

-- housekeeping: reservations that outlived their offer (cancelled/finished tournaments, resolved offers)
create or replace function ipl_release_stale_reservations() returns int
language plpgsql security definer set search_path = public, ipl_private as $$
declare n int;
begin
  delete from ipl_player_reservations x
   where not exists (select 1 from ipl_draft_offers o where o.id = x.offer_id and o.status = 'OPEN')
      or exists (select 1 from ipl_tournaments t where t.id = x.tournament_id and t.state in ('COMPLETED','CANCELLED'));
  get diagnostics n = row_count;
  return n;
end $$;

-- ───────────── system franchises ─────────────
create or replace function ipl_begin_system_teams(p_tid bigint, p_franchises jsonb) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype; h int; f jsonb; i int := 0;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found then return jsonb_build_object('status', 'NOT_FOUND'); end if;
  if t.state = 'SYSTEM_TEAM_GENERATION' then return jsonb_build_object('status', 'OK', 'already', true); end if;
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

create or replace function ipl_system_progress(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(jsonb_build_object('team_id', tm.id, 'code', tm.franchise_code, 'team_no', tm.team_no,
           'picks', (select count(*) from ipl_draft_picks k where k.team_id = tm.id), 'done', tm.squad_complete,
           'roster', coalesce((select jsonb_agg(jsonb_build_object('role', p.role, 'bowling_type', p.bowling_type))
                                 from ipl_team_rosters r join ipl_players p on p.id = r.player_id
                                where r.team_id = tm.id), '[]'::jsonb))
           order by tm.team_no), '[]'::jsonb)
    from ipl_tournament_teams tm where tm.tournament_id = p_tid and tm.kind = 'SYSTEM'
$$;

-- AI-only: private (hidden) rating of each card in an open system offer, so the franchise AI can choose
create or replace function ipl_system_offer_ratings(p_offer_id bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(jsonb_build_object('pos', c.position, 'player_id', c.player_id,
           'role', p.role, 'bowling_type', p.bowling_type,
           'ratings', to_jsonb(r) - 'tournament_id' - 'player_id')  order by c.position), '[]'::jsonb)
    from ipl_draft_offers o
    join ipl_draft_offer_candidates c on c.offer_id = o.id
    join ipl_players p on p.id = c.player_id
    join ipl_private.ipl_tournament_ratings r on r.tournament_id = o.tournament_id and r.player_id = c.player_id
   where o.id = p_offer_id
$$;

create or replace function ipl_complete_system_teams(p_tid bigint) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype; bad int;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found then return jsonb_build_object('status', 'NOT_FOUND'); end if;
  if t.state = 'FIXTURE_GENERATION' then return jsonb_build_object('status', 'OK', 'already', true); end if;
  if t.state not in ('DRAFTING', 'SYSTEM_TEAM_GENERATION') then return jsonb_build_object('status', 'BAD_STATE', 'state', t.state); end if;
  select count(*) into bad from ipl_tournament_teams tm
   where tm.tournament_id = p_tid and (select count(*) from ipl_team_rosters r where r.team_id = tm.id) <> 11;
  if bad > 0 then return jsonb_build_object('status', 'INCOMPLETE', 'teams', bad); end if;
  update ipl_tournament_teams set squad_complete = true where tournament_id = p_tid and not squad_complete;
  if t.state = 'SYSTEM_TEAM_GENERATION' then
    update ipl_tournaments set state = 'FIXTURE_GENERATION' where id = p_tid;
  end if;
  return jsonb_build_object('status', 'OK');
end $$;

-- ───────────── engine inputs (HIDDEN ratings: trusted server code only) ─────────────
create or replace function ipl_engine_squads(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(jsonb_build_object(
      'team_id', tm.id, 'team_no', tm.team_no, 'name', tm.name, 'short_name', tm.short_name, 'kind', tm.kind,
      'players', (select coalesce(jsonb_agg(jsonb_build_object(
                    'id', p.id, 'name', p.display_name, 'role', p.role, 'bowling_type', p.bowling_type,
                    'batting_style', p.batting_style, 'slot', r.slot,
                    'batting_order', r.batting_order,
                    'ratings', to_jsonb(tr) - 'tournament_id' - 'player_id') order by coalesce(r.batting_order, r.slot)), '[]'::jsonb)
                    from ipl_team_rosters r
                    join ipl_players p on p.id = r.player_id
                    left join ipl_private.ipl_tournament_ratings tr
                           on tr.tournament_id = r.tournament_id and tr.player_id = r.player_id
                   where r.team_id = tm.id)) order by tm.team_no), '[]'::jsonb)
    from ipl_tournament_teams tm where tm.tournament_id = p_tid
$$;

-- ───────────── fixtures ─────────────
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

-- claim up to p_limit due fixtures (lowest match_no first); a claim older than p_stale seconds is re-claimable
create or replace function ipl_claim_fixtures(p_tid bigint, p_owner text, p_limit int, p_stale int default 300)
returns jsonb language plpgsql security definer set search_path = public, ipl_private as $$
declare ids bigint[]; md int; st text;
begin
  select state into st from ipl_tournaments where id = p_tid;
  if st is null or st not in ('LEAGUE_RUNNING','PLAYOFF_ELIMINATOR','PLAYOFF_QUALIFIER_1','PLAYOFF_QUALIFIER_2','PLAYOFF_FINAL') then
    return '[]'::jsonb;
  end if;
  select array_agg(id) into ids from (
    select f.id from ipl_fixtures f
     where f.tournament_id = p_tid
       and (f.status = 'SCHEDULED' or (f.status = 'CLAIMED' and f.claimed_at < now() - make_interval(secs => p_stale)))
     order by f.match_no limit p_limit for update skip locked) s;
  if ids is null then return '[]'::jsonb; end if;
  update ipl_fixtures set status = 'CLAIMED', claimed_by = p_owner, claimed_at = now() where id = any(ids);
  return (select coalesce(jsonb_agg(jsonb_build_object('id', f.id, 'stage', f.stage, 'match_no', f.match_no,
            'matchday', f.matchday, 'leg', f.leg, 'home', f.home_team_id, 'away', f.away_team_id) order by f.match_no), '[]'::jsonb)
            from ipl_fixtures f where f.id = any(ids));
end $$;

create or replace function ipl_release_claims(p_tid bigint, p_owner text) returns int
language plpgsql security definer set search_path = public, ipl_private as $$
declare n int;
begin
  update ipl_fixtures set status = 'SCHEDULED', claimed_by = null, claimed_at = null
   where tournament_id = p_tid and status = 'CLAIMED' and claimed_by = p_owner;
  get diagnostics n = row_count; return n;
end $$;

-- ───────────── settle a match (atomic, idempotent) ─────────────
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

  insert into ipl_matches (fixture_id, tournament_id, stage, match_no, team1_id, team2_id, toss_winner_id, toss_decision,
                           winner_team_id, result_type, margin, super_overs, pom_player_id, best_bowler_id, summary, rng_seed)
  values (f.id, f.tournament_id, f.stage, f.match_no, t1, t2, nullif(p_result->>'toss_winner_id','')::bigint,
          nullif(p_result->>'toss_decision',''), w, rt, nullif(p_result->>'margin','')::int,
          coalesce((p_result->>'super_overs')::smallint, 0), nullif(p_result->>'pom_player_id','')::uuid,
          nullif(p_result->>'best_bowler_id','')::uuid, p_result->>'summary', p_result->>'seed')
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

-- ───────────── standings / league completion / bracket ─────────────
create or replace function ipl_standings_json(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(jsonb_build_object('team_id', s.team_id, 'name', tm.name, 'short_name', tm.short_name,
           'kind', tm.kind, 'user_id', tm.user_id, 'team_no', tm.team_no, 'played', s.played, 'won', s.won, 'lost', s.lost,
           'no_result', s.no_result, 'points', s.points, 'runs_for', s.runs_for, 'balls_faced', s.balls_faced,
           'runs_against', s.runs_against, 'balls_bowled', s.balls_bowled, 'nrr', s.nrr, 'rank', s.rank)
           order by s.rank nulls last, s.points desc, s.nrr desc, s.won desc, tm.team_no), '[]'::jsonb)
    from ipl_standings s join ipl_tournament_teams tm on tm.id = s.team_id where s.tournament_id = p_tid
$$;

create or replace function ipl_league_results(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(jsonb_build_object('team1', m.team1_id, 'team2', m.team2_id, 'winner', m.winner_team_id)), '[]'::jsonb)
    from ipl_matches m where m.tournament_id = p_tid and m.stage = 'LEAGUE'
$$;

create or replace function ipl_league_progress(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select jsonb_build_object(
    'total',  (select count(*) from ipl_fixtures where tournament_id = p_tid and stage = 'LEAGUE'),
    'done',   (select count(*) from ipl_fixtures where tournament_id = p_tid and stage = 'LEAGUE' and status = 'COMPLETED'),
    'matchdays', (select coalesce(max(matchday), 0) from ipl_fixtures where tournament_id = p_tid and stage = 'LEAGUE'),
    'completed_matchdays', (select coalesce(max(md), 0) from (
         select f.matchday as md from ipl_fixtures f where f.tournament_id = p_tid and f.stage = 'LEAGUE'
         group by f.matchday having bool_and(f.status = 'COMPLETED')) z where md <= (
            select coalesce(min(f2.matchday) - 1, 1000000) from ipl_fixtures f2
             where f2.tournament_id = p_tid and f2.stage = 'LEAGUE' and f2.status <> 'COMPLETED')),
    'last_posted', (select last_posted_matchday from ipl_tournaments where id = p_tid))
$$;

create or replace function ipl_mark_matchday_posted(p_tid bigint, p_matchday int) returns void
language sql security definer set search_path = public, ipl_private as $$
  update ipl_tournaments set last_posted_matchday = greatest(last_posted_matchday, p_matchday) where id = p_tid
$$;

create or replace function ipl_complete_league(p_tid bigint, p_ranking jsonb) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype; n int; r jsonb; i int := 0; base int; ids bigint[]; fx bigint;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found then return jsonb_build_object('status', 'NOT_FOUND'); end if;
  if t.state <> 'LEAGUE_RUNNING' then return jsonb_build_object('status', 'BAD_STATE', 'state', t.state); end if;
  if exists (select 1 from ipl_fixtures where tournament_id = p_tid and stage = 'LEAGUE' and status <> 'COMPLETED') then
    return jsonb_build_object('status', 'LEAGUE_NOT_DONE');
  end if;
  select count(*) into n from ipl_tournament_teams where tournament_id = p_tid;
  if jsonb_array_length(p_ranking) <> n then return jsonb_build_object('status', 'BAD_RANKING'); end if;
  for r in select * from jsonb_array_elements(p_ranking) loop
    i := i + 1;
    update ipl_standings set rank = i where tournament_id = p_tid and team_id = (r#>>'{}')::bigint;
    ids := array_append(ids, (r#>>'{}')::bigint);
  end loop;
  select max(match_no) into base from ipl_fixtures where tournament_id = p_tid;
  insert into ipl_playoff_matches (tournament_id, stage, slot, home_source, away_source) values
    (p_tid, 'ELIMINATOR',  1, 'RANK3', 'RANK4'),
    (p_tid, 'QUALIFIER_1', 2, 'RANK1', 'RANK2'),
    (p_tid, 'QUALIFIER_2', 3, 'LOSER_Q1', 'WINNER_ELIMINATOR'),
    (p_tid, 'FINAL',       4, 'WINNER_Q1', 'WINNER_Q2') on conflict do nothing;
  insert into ipl_fixtures (tournament_id, stage, match_no, home_team_id, away_team_id)
    values (p_tid, 'ELIMINATOR', base + 1, ids[3], ids[4]) returning id into fx;
  update ipl_playoff_matches set fixture_id = fx where tournament_id = p_tid and stage = 'ELIMINATOR';
  insert into ipl_fixtures (tournament_id, stage, match_no, home_team_id, away_team_id)
    values (p_tid, 'QUALIFIER_1', base + 2, ids[1], ids[2]) returning id into fx;
  update ipl_playoff_matches set fixture_id = fx where tournament_id = p_tid and stage = 'QUALIFIER_1';
  update ipl_tournaments set state = 'LEAGUE_COMPLETED' where id = p_tid;
  return jsonb_build_object('status', 'OK');
end $$;

create or replace function ipl_begin_playoffs(p_tid bigint) returns boolean
language plpgsql security definer set search_path = public, ipl_private as $$
begin
  update ipl_tournaments set state = 'PLAYOFF_ELIMINATOR' where id = p_tid and state = 'LEAGUE_COMPLETED';
  return found;
end $$;

create or replace function ipl_playoff_bracket(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(jsonb_build_object('stage', pm.stage, 'slot', pm.slot,
           'home_source', pm.home_source, 'away_source', pm.away_source, 'fixture_id', pm.fixture_id,
           'status', f.status, 'home', ipl_team_json(h), 'away', ipl_team_json(a),
           'match_id', m.id, 'winner_team_id', m.winner_team_id) order by pm.slot), '[]'::jsonb)
    from ipl_playoff_matches pm
    left join ipl_fixtures f on f.id = pm.fixture_id
    left join ipl_tournament_teams h on h.id = f.home_team_id
    left join ipl_tournament_teams a on a.id = f.away_team_id
    left join ipl_matches m on m.fixture_id = f.id
   where pm.tournament_id = p_tid
$$;

-- ───────────── match / fixture displays ─────────────
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
                                 where i.match_id = m.id and not i.is_super_over and b.player_id = p.id), 0),
              'balls', coalesce((select sum(b.balls) from ipl_batting_scorecards b join ipl_innings i on i.id = b.innings_id
                                  where i.match_id = m.id and not i.is_super_over and b.player_id = p.id), 0),
              'wickets', coalesce((select sum(w.wickets) from ipl_bowling_scorecards w join ipl_innings i on i.id = w.innings_id
                                    where i.match_id = m.id and not i.is_super_over and w.player_id = p.id), 0),
              'bowl_runs', coalesce((select sum(w.runs) from ipl_bowling_scorecards w join ipl_innings i on i.id = w.innings_id
                                      where i.match_id = m.id and not i.is_super_over and w.player_id = p.id), 0),
              'team_id', (select r.team_id from ipl_team_rosters r where r.tournament_id = m.tournament_id and r.player_id = p.id))
             from ipl_players p where p.id = m.pom_player_id),
    'best_bowler', (select jsonb_build_object('player_id', p.id, 'name', p.display_name,
              'wickets', coalesce((select sum(w.wickets) from ipl_bowling_scorecards w join ipl_innings i on i.id = w.innings_id
                                    where i.match_id = m.id and not i.is_super_over and w.player_id = p.id), 0),
              'runs', coalesce((select sum(w.runs) from ipl_bowling_scorecards w join ipl_innings i on i.id = w.innings_id
                                 where i.match_id = m.id and not i.is_super_over and w.player_id = p.id), 0))
             from ipl_players p where p.id = m.best_bowler_id))
    from ipl_matches m
    join ipl_tournament_teams t1 on t1.id = m.team1_id
    join ipl_tournament_teams t2 on t2.id = m.team2_id
   where m.id = p_match_id
$$;

create or replace function ipl_match_ids_for_matchday(p_tid bigint, p_matchday int) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(m.id order by m.match_no), '[]'::jsonb)
    from ipl_matches m join ipl_fixtures f on f.id = m.fixture_id
   where m.tournament_id = p_tid and f.stage = 'LEAGUE' and f.matchday = p_matchday
$$;

create or replace function ipl_match_scorecard(p_match_id bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select ipl_match_card(m.id) || jsonb_build_object('innings_detail', coalesce((
    select jsonb_agg(jsonb_build_object(
      'innings_no', i.innings_no, 'is_super_over', i.is_super_over, 'runs', i.runs, 'wickets', i.wickets,
      'legal_balls', i.legal_balls, 'extras', i.extras, 'fall_of_wickets', i.fall_of_wickets, 'target', i.target,
      'batting_team', ipl_team_json(bt),
      'batting', (select coalesce(jsonb_agg(jsonb_build_object('name', p.display_name, 'runs', b.runs, 'balls', b.balls,
                    'fours', b.fours, 'sixes', b.sixes, 'out', b.is_out, 'dismissal', b.dismissal, 'did_bat', b.did_bat,
                    'bowler', (select display_name from ipl_players where id = b.bowler_id),
                    'fielder', (select display_name from ipl_players where id = b.fielder_id)) order by b.batting_pos), '[]'::jsonb)
                    from ipl_batting_scorecards b join ipl_players p on p.id = b.player_id where b.innings_id = i.id),
      'bowling', (select coalesce(jsonb_agg(jsonb_build_object('name', p.display_name, 'legal_balls', w.legal_balls,
                    'runs', w.runs, 'wickets', w.wickets, 'maidens', w.maidens, 'wides', w.wides, 'noballs', w.noballs)
                    order by w.legal_balls desc, w.wickets desc), '[]'::jsonb)
                    from ipl_bowling_scorecards w join ipl_players p on p.id = w.player_id where w.innings_id = i.id)
    ) order by i.innings_no)
    from ipl_innings i join ipl_tournament_teams bt on bt.id = i.batting_team_id where i.match_id = m.id), '[]'::jsonb))
    from ipl_matches m where m.id = p_match_id
$$;

create or replace function ipl_fixtures_page(p_tid bigint, p_offset int, p_limit int, p_team_id bigint default null)
returns jsonb language sql stable security definer set search_path = public, ipl_private as $$
  select jsonb_build_object('total', (select count(*) from ipl_fixtures f where f.tournament_id = p_tid
                                         and (p_team_id is null or p_team_id in (f.home_team_id, f.away_team_id))),
    'rows', coalesce((select jsonb_agg(x) from (
      select jsonb_build_object('fixture_id', f.id, 'stage', f.stage, 'match_no', f.match_no, 'matchday', f.matchday,
               'status', f.status, 'home', h.short_name, 'away', a.short_name, 'home_name', h.name, 'away_name', a.name,
               'match_id', m.id, 'winner_short', (select short_name from ipl_tournament_teams where id = m.winner_team_id)) x
        from ipl_fixtures f
        join ipl_tournament_teams h on h.id = f.home_team_id
        join ipl_tournament_teams a on a.id = f.away_team_id
        left join ipl_matches m on m.fixture_id = f.id
       where f.tournament_id = p_tid and (p_team_id is null or p_team_id in (f.home_team_id, f.away_team_id))
       order by f.match_no offset p_offset limit p_limit) s), '[]'::jsonb))
$$;

create or replace function ipl_matches_page(p_tid bigint, p_offset int, p_limit int, p_team_id bigint default null)
returns jsonb language sql stable security definer set search_path = public, ipl_private as $$
  select jsonb_build_object('total', (select count(*) from ipl_matches m where m.tournament_id = p_tid
                                         and (p_team_id is null or p_team_id in (m.team1_id, m.team2_id))),
    'rows', coalesce((select jsonb_agg(ipl_match_card(s.id) order by s.match_no desc) from (
      select m.id, m.match_no from ipl_matches m where m.tournament_id = p_tid
         and (p_team_id is null or p_team_id in (m.team1_id, m.team2_id))
       order by m.match_no desc offset p_offset limit p_limit) s), '[]'::jsonb))
$$;

-- ───────────── awards ─────────────
create or replace function ipl_player_tournament_stats(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  with inn as (
    select i.id, i.match_id, i.batting_team_id, i.bowling_team_id
      from ipl_innings i join ipl_matches m on m.id = i.match_id
     where m.tournament_id = p_tid and not i.is_super_over),
  bat as (
    select b.player_id, count(*) filter (where b.did_bat) as innings, sum(b.runs) runs, sum(b.balls) balls,
           sum(b.fours) fours, sum(b.sixes) sixes, count(*) filter (where b.is_out) outs,
           max(b.runs) high,
           (array_agg(b.balls order by b.runs desc, b.balls asc))[1] as high_balls
      from ipl_batting_scorecards b join inn on inn.id = b.innings_id where b.did_bat group by b.player_id),
  bowl as (
    select w.player_id, count(*) filter (where w.legal_balls > 0) as innings, sum(w.legal_balls) balls, sum(w.runs) runs,
           sum(w.wickets) wickets, sum(w.maidens) maidens,
           max(w.wickets) best_w
      from ipl_bowling_scorecards w join inn on inn.id = w.innings_id group by w.player_id),
  fld as (
    select b.fielder_id as player_id,
           count(*) filter (where b.dismissal = 'caught') catches,
           count(*) filter (where b.dismissal = 'stumped') stumpings,
           count(*) filter (where b.dismissal = 'run out') run_outs
      from ipl_batting_scorecards b join inn on inn.id = b.innings_id
     where b.fielder_id is not null group by b.fielder_id),
  pom as (
    select m.pom_player_id as player_id, count(*) n from ipl_matches m
     where m.tournament_id = p_tid and m.pom_player_id is not null group by m.pom_player_id),
  everyone as (
    select player_id from bat union select player_id from bowl union select player_id from fld union select player_id from pom)
  select coalesce(jsonb_agg(jsonb_build_object(
      'player_id', e.player_id, 'name', p.display_name,
      'team_id', (select r.team_id from ipl_team_rosters r where r.tournament_id = p_tid and r.player_id = e.player_id),
      'bat_innings', coalesce(bat.innings, 0), 'runs', coalesce(bat.runs, 0), 'balls', coalesce(bat.balls, 0),
      'fours', coalesce(bat.fours, 0), 'sixes', coalesce(bat.sixes, 0), 'outs', coalesce(bat.outs, 0),
      'high', coalesce(bat.high, 0), 'high_balls', coalesce(bat.high_balls, 0),
      'bowl_balls', coalesce(bowl.balls, 0), 'bowl_runs', coalesce(bowl.runs, 0), 'wickets', coalesce(bowl.wickets, 0),
      'maidens', coalesce(bowl.maidens, 0), 'best_w', coalesce(bowl.best_w, 0),
      'catches', coalesce(fld.catches, 0), 'stumpings', coalesce(fld.stumpings, 0), 'run_outs', coalesce(fld.run_outs, 0),
      'pom', coalesce(pom.n, 0))), '[]'::jsonb)
    from everyone e
    join ipl_players p on p.id = e.player_id
    left join bat  on bat.player_id  = e.player_id
    left join bowl on bowl.player_id = e.player_id
    left join fld  on fld.player_id  = e.player_id
    left join pom  on pom.player_id  = e.player_id
$$;

create or replace function ipl_store_awards(p_tid bigint, p_awards jsonb) returns int
language plpgsql security definer set search_path = public, ipl_private as $$
declare a jsonb; n int := 0;
begin
  for a in select * from jsonb_array_elements(p_awards) loop
    insert into ipl_tournament_awards (tournament_id, award, player_id, team_id, value_text, value_num)
    values (p_tid, a->>'award', nullif(a->>'player_id','')::uuid, nullif(a->>'team_id','')::bigint,
            a->>'value_text', nullif(a->>'value_num','')::numeric)
    on conflict (tournament_id, award) do update set player_id = excluded.player_id, team_id = excluded.team_id,
         value_text = excluded.value_text, value_num = excluded.value_num;
    n := n + 1;
  end loop;
  return n;
end $$;

create or replace function ipl_get_awards(p_tid bigint) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(jsonb_build_object('award', a.award, 'player', p.display_name, 'player_id', a.player_id,
           'team', tm.name, 'team_id', a.team_id, 'value_text', a.value_text, 'value_num', a.value_num)), '[]'::jsonb)
    from ipl_tournament_awards a
    left join ipl_players p on p.id = a.player_id
    left join ipl_tournament_teams tm on tm.id = a.team_id
   where a.tournament_id = p_tid
$$;

-- ───────────── leaderboards ─────────────
-- Idempotent: one call per tournament, guarded by the row lock + leaderboard_processed flag.
create or replace function ipl_record_results(p_tid bigint) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare t ipl_tournaments%rowtype; r record;
begin
  select * into t from ipl_tournaments where id = p_tid for update;
  if not found or t.state <> 'COMPLETED' then return jsonb_build_object('status', 'NOT_COMPLETED'); end if;
  if t.leaderboard_processed then return jsonb_build_object('status', 'ALREADY'); end if;

  insert into ipl_tournament_results (tournament_id, team_id, user_id, group_id, league_rank, champion, runner_up, playoff,
                                      league_won, league_lost, league_played, runs_scored, wickets_taken)
  select tm.tournament_id, tm.id, tm.user_id, t.group_id, s.rank,
         tm.id = t.champion_team_id, tm.id = t.runner_up_team_id,
         exists (select 1 from ipl_fixtures f where f.tournament_id = tm.tournament_id and f.stage <> 'LEAGUE'
                  and tm.id in (f.home_team_id, f.away_team_id)),
         s.won, s.lost, s.played,
         coalesce((select sum(b.runs) from ipl_batting_scorecards b join ipl_innings i on i.id = b.innings_id
                    join ipl_matches m on m.id = i.match_id
                   where m.tournament_id = tm.tournament_id and i.batting_team_id = tm.id and not i.is_super_over), 0),
         coalesce((select sum(w.wickets) from ipl_bowling_scorecards w join ipl_innings i on i.id = w.innings_id
                    join ipl_matches m on m.id = i.match_id
                   where m.tournament_id = tm.tournament_id and i.bowling_team_id = tm.id and not i.is_super_over), 0)
    from ipl_tournament_teams tm
    join ipl_standings s on s.tournament_id = tm.tournament_id and s.team_id = tm.id
   where tm.tournament_id = p_tid and tm.kind = 'HUMAN'
  on conflict do nothing;

  for r in select * from ipl_tournament_results where tournament_id = p_tid loop
    insert into ipl_user_stats as u (user_id, tournaments_played, championships, runner_ups, playoff_appearances,
                                     league_wins, league_losses, league_played, runs_scored, wickets_taken)
    values (r.user_id, 1, r.champion::int, r.runner_up::int, r.playoff::int, r.league_won, r.league_lost,
            r.league_played, r.runs_scored, r.wickets_taken)
    on conflict (user_id) do update set
      tournaments_played = u.tournaments_played + 1, championships = u.championships + excluded.championships,
      runner_ups = u.runner_ups + excluded.runner_ups, playoff_appearances = u.playoff_appearances + excluded.playoff_appearances,
      league_wins = u.league_wins + excluded.league_wins, league_losses = u.league_losses + excluded.league_losses,
      league_played = u.league_played + excluded.league_played, runs_scored = u.runs_scored + excluded.runs_scored,
      wickets_taken = u.wickets_taken + excluded.wickets_taken, updated_at = now();
    insert into ipl_group_stats as g (group_id, user_id, tournaments_played, championships, runner_ups, playoff_appearances,
                                      league_wins, league_losses, league_played, runs_scored, wickets_taken)
    values (r.group_id, r.user_id, 1, r.champion::int, r.runner_up::int, r.playoff::int, r.league_won, r.league_lost,
            r.league_played, r.runs_scored, r.wickets_taken)
    on conflict (group_id, user_id) do update set
      tournaments_played = g.tournaments_played + 1, championships = g.championships + excluded.championships,
      runner_ups = g.runner_ups + excluded.runner_ups, playoff_appearances = g.playoff_appearances + excluded.playoff_appearances,
      league_wins = g.league_wins + excluded.league_wins, league_losses = g.league_losses + excluded.league_losses,
      league_played = g.league_played + excluded.league_played, runs_scored = g.runs_scored + excluded.runs_scored,
      wickets_taken = g.wickets_taken + excluded.wickets_taken, updated_at = now();
  end loop;
  update ipl_tournaments set leaderboard_processed = true where id = p_tid;
  return jsonb_build_object('status', 'OK');
end $$;

-- Repair tool: rebuild both stats tables from ipl_tournament_results (never mixes groups or games).
create or replace function ipl_rebuild_stats() returns void
language plpgsql security definer set search_path = public, ipl_private as $$
begin
  truncate ipl_user_stats, ipl_group_stats;
  insert into ipl_user_stats (user_id, tournaments_played, championships, runner_ups, playoff_appearances,
                              league_wins, league_losses, league_played, runs_scored, wickets_taken)
    select user_id, count(*), count(*) filter (where champion), count(*) filter (where runner_up),
           count(*) filter (where playoff), sum(league_won), sum(league_lost), sum(league_played),
           sum(runs_scored), sum(wickets_taken) from ipl_tournament_results group by user_id;
  insert into ipl_group_stats (group_id, user_id, tournaments_played, championships, runner_ups, playoff_appearances,
                               league_wins, league_losses, league_played, runs_scored, wickets_taken)
    select group_id, user_id, count(*), count(*) filter (where champion), count(*) filter (where runner_up),
           count(*) filter (where playoff), sum(league_won), sum(league_lost), sum(league_played),
           sum(runs_scored), sum(wickets_taken) from ipl_tournament_results group by group_id, user_id;
end $$;

create or replace function ipl_global_leaderboard(p_user_id bigint, p_limit int, p_offset int) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  with ranked as (
    select s.*, u.first_name, u.username,
           dense_rank() over (order by s.championships desc, s.runner_ups desc, s.league_wins desc) rnk
      from ipl_user_stats s join users u on u.id = s.user_id)
  select jsonb_build_object(
    'total', (select count(*) from ranked),
    'rows', coalesce((select jsonb_agg(to_jsonb(x) order by x.rnk, x.user_id) from (
        select * from ranked order by rnk, user_id offset p_offset limit p_limit) x), '[]'::jsonb),
    'me', (select to_jsonb(r) from ranked r where r.user_id = p_user_id))
$$;

create or replace function ipl_group_leaderboard(p_group_id bigint, p_user_id bigint, p_limit int, p_offset int) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  with ranked as (
    select s.*, u.first_name, u.username,
           dense_rank() over (order by s.championships desc, s.runner_ups desc, s.league_wins desc) rnk
      from ipl_group_stats s join users u on u.id = s.user_id where s.group_id = p_group_id)
  select jsonb_build_object(
    'total', (select count(*) from ranked),
    'rows', coalesce((select jsonb_agg(to_jsonb(x) order by x.rnk, x.user_id) from (
        select * from ranked order by rnk, user_id offset p_offset limit p_limit) x), '[]'::jsonb),
    'me', (select to_jsonb(r) from ranked r where r.user_id = p_user_id))
$$;

create or replace function ipl_user_stats_json(p_user_id bigint, p_group_id bigint default null) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select jsonb_build_object('global', (select to_jsonb(s) from ipl_user_stats s where s.user_id = p_user_id),
                            'group',  (select to_jsonb(g) from ipl_group_stats g where g.user_id = p_user_id and g.group_id = p_group_id))
$$;

create or replace function ipl_user_has_group_stats(p_user_id bigint, p_group_id bigint) returns boolean
language sql stable security definer set search_path = public, ipl_private as $$
  select exists (select 1 from ipl_group_stats where user_id = p_user_id and group_id = p_group_id)
$$;

create or replace function ipl_groups_of_user(p_user_id bigint, p_limit int default 10) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(jsonb_build_object('group_id', g.group_id, 'title', gr.title, 'tournaments_played', g.tournaments_played)
           order by g.updated_at desc), '[]'::jsonb)
    from (select * from ipl_group_stats where user_id = p_user_id order by updated_at desc limit p_limit) g
    join groups gr on gr.id = g.group_id
$$;

create or replace function ipl_group_history(p_group_id bigint, p_limit int default 10) returns jsonb
language sql stable security definer set search_path = public, ipl_private as $$
  select coalesce(jsonb_agg(jsonb_build_object('tournament_id', t.id, 'completed_at', t.completed_at,
           'champion', c.name, 'champion_kind', c.kind, 'champion_owner', c.owner_name,
           'runner_up', r.name, 'humans', (select count(*) from ipl_tournament_teams x where x.tournament_id = t.id and x.kind = 'HUMAN'))
           order by t.id desc), '[]'::jsonb)
    from (select * from ipl_tournaments where group_id = p_group_id and state = 'COMPLETED' order by id desc limit p_limit) t
    left join ipl_tournament_teams c on c.id = t.champion_team_id
    left join ipl_tournament_teams r on r.id = t.runner_up_team_id
$$;

-- ───────────── player import (idempotent) ─────────────
-- p_source: {name, data_version, url, license, notes}
-- p_players: [{slug, full_name, display_name, name_key, cricsheet_id?, role, batting_style?, bowling_type, bowling_style?,
--              nationality, first_ipl_season?, last_ipl_season?, is_eligible, ipl_verified, data_quality,
--              career?: {...}, seasons?: [...], ratings: {13 attrs, rating_source, method_version}}]
-- Re-running never duplicates a player (slug / cricsheet_id upsert) and never touches tournament snapshots.
create or replace function ipl_import_players(p_source jsonb, p_players jsonb) returns jsonb
language plpgsql security definer set search_path = public, ipl_private as $$
declare sid bigint; p jsonb; pid uuid; n_new int := 0; n_upd int := 0; s jsonb; c jsonb; rt jsonb; existed boolean;
begin
  insert into ipl_player_stat_sources (name, data_version, url, license, notes, record_count, fetched_at)
  values (p_source->>'name', coalesce(p_source->>'data_version', ''), p_source->>'url', p_source->>'license',
          p_source->>'notes', jsonb_array_length(p_players), now())
  on conflict (name, data_version) do update set url = excluded.url, license = excluded.license, notes = excluded.notes,
       record_count = excluded.record_count, fetched_at = now()
  returning id into sid;

  for p in select * from jsonb_array_elements(p_players) loop
    select id into pid from ipl_players
     where (p->>'cricsheet_id' is not null and cricsheet_id = p->>'cricsheet_id') or slug = p->>'slug' limit 1;
    existed := pid is not null;
    if not existed then
      insert into ipl_players (slug, full_name, display_name, name_key, cricsheet_id, role, batting_style, bowling_type,
                               bowling_style, nationality, first_ipl_season, last_ipl_season, is_eligible, ipl_verified,
                               data_quality, source_id)
      values (p->>'slug', p->>'full_name', p->>'display_name', p->>'name_key', nullif(p->>'cricsheet_id',''), p->>'role',
              nullif(p->>'batting_style',''), coalesce(p->>'bowling_type','NONE'), nullif(p->>'bowling_style',''),
              coalesce(p->>'nationality',''), nullif(p->>'first_ipl_season','')::smallint,
              nullif(p->>'last_ipl_season','')::smallint, coalesce((p->>'is_eligible')::boolean, true),
              coalesce((p->>'ipl_verified')::boolean, false), coalesce(p->>'data_quality', 'SEED'), sid)
      returning id into pid;
      n_new := n_new + 1;
    else
      update ipl_players set
        full_name = p->>'full_name', display_name = p->>'display_name', name_key = p->>'name_key',
        cricsheet_id = coalesce(nullif(p->>'cricsheet_id',''), cricsheet_id), role = p->>'role',
        batting_style = nullif(p->>'batting_style',''), bowling_type = coalesce(p->>'bowling_type','NONE'),
        bowling_style = nullif(p->>'bowling_style',''), nationality = coalesce(p->>'nationality',''),
        first_ipl_season = nullif(p->>'first_ipl_season','')::smallint, last_ipl_season = nullif(p->>'last_ipl_season','')::smallint,
        is_eligible = coalesce((p->>'is_eligible')::boolean, true),
        ipl_verified = coalesce((p->>'ipl_verified')::boolean, false) or ipl_verified,
        -- never downgrade real (CRICSHEET) data with a seed row
        data_quality = case when data_quality = 'CRICSHEET' then 'CRICSHEET' else coalesce(p->>'data_quality', 'SEED') end,
        source_id = sid, updated_at = now()
       where id = pid;
      n_upd := n_upd + 1;
    end if;

    c := p->'career';
    if c is not null and c <> 'null'::jsonb then
      insert into ipl_player_career_stats as cs (player_id, matches, innings_bat, runs, balls_faced, outs, fours, sixes,
             highest_score, fifties, hundreds, innings_bowl, balls_bowled, runs_conceded, wickets, best_bowling,
             catches, stumpings, run_outs, updated_at)
      values (pid, (c->>'matches')::int, (c->>'innings_bat')::int, (c->>'runs')::int, (c->>'balls_faced')::int,
              (c->>'outs')::int, (c->>'fours')::int, (c->>'sixes')::int, (c->>'highest_score')::int,
              (c->>'fifties')::int, (c->>'hundreds')::int, (c->>'innings_bowl')::int, (c->>'balls_bowled')::int,
              (c->>'runs_conceded')::int, (c->>'wickets')::int, c->>'best_bowling', (c->>'catches')::int,
              (c->>'stumpings')::int, (c->>'run_outs')::int, now())
      on conflict (player_id) do update set matches = excluded.matches, innings_bat = excluded.innings_bat,
        runs = excluded.runs, balls_faced = excluded.balls_faced, outs = excluded.outs, fours = excluded.fours,
        sixes = excluded.sixes, highest_score = excluded.highest_score, fifties = excluded.fifties,
        hundreds = excluded.hundreds, innings_bowl = excluded.innings_bowl, balls_bowled = excluded.balls_bowled,
        runs_conceded = excluded.runs_conceded, wickets = excluded.wickets, best_bowling = excluded.best_bowling,
        catches = excluded.catches, stumpings = excluded.stumpings, run_outs = excluded.run_outs, updated_at = now();
    end if;

    if p->'seasons' is not null and p->'seasons' <> 'null'::jsonb then
      for s in select * from jsonb_array_elements(p->'seasons') loop
        insert into ipl_player_seasons (player_id, season, matches, runs, balls_faced, outs, fours, sixes,
                                        balls_bowled, runs_conceded, wickets)
        values (pid, (s->>'season')::smallint, (s->>'matches')::int, (s->>'runs')::int, (s->>'balls_faced')::int,
                (s->>'outs')::int, (s->>'fours')::int, (s->>'sixes')::int, (s->>'balls_bowled')::int,
                (s->>'runs_conceded')::int, (s->>'wickets')::int)
        on conflict (player_id, season) do update set matches = excluded.matches, runs = excluded.runs,
          balls_faced = excluded.balls_faced, outs = excluded.outs, fours = excluded.fours, sixes = excluded.sixes,
          balls_bowled = excluded.balls_bowled, runs_conceded = excluded.runs_conceded, wickets = excluded.wickets;
      end loop;
    end if;

    rt := p->'ratings';
    if rt is not null and rt <> 'null'::jsonb then
      -- real-stat ratings are never replaced by an editorial seed prior
      if rt->>'rating_source' = 'CRICSHEET_STATS' or not exists (
           select 1 from ipl_private.ipl_player_private_ratings where player_id = pid and rating_source = 'CRICSHEET_STATS') then
        insert into ipl_private.ipl_player_private_ratings as r (player_id, batting_rating, bowling_rating, fielding_rating,
               wicketkeeping_rating, batting_consistency, bowling_consistency, power_hitting, strike_rotation,
               death_overs_batting, death_overs_bowling, spin_effectiveness, pace_effectiveness, overall_rating,
               rating_source, method_version, computed_at)
        values (pid, (rt->>'batting_rating')::smallint, (rt->>'bowling_rating')::smallint, (rt->>'fielding_rating')::smallint,
                (rt->>'wicketkeeping_rating')::smallint, (rt->>'batting_consistency')::smallint,
                (rt->>'bowling_consistency')::smallint, (rt->>'power_hitting')::smallint, (rt->>'strike_rotation')::smallint,
                (rt->>'death_overs_batting')::smallint, (rt->>'death_overs_bowling')::smallint,
                (rt->>'spin_effectiveness')::smallint, (rt->>'pace_effectiveness')::smallint,
                (rt->>'overall_rating')::smallint, rt->>'rating_source', rt->>'method_version', now())
        on conflict (player_id) do update set batting_rating = excluded.batting_rating, bowling_rating = excluded.bowling_rating,
          fielding_rating = excluded.fielding_rating, wicketkeeping_rating = excluded.wicketkeeping_rating,
          batting_consistency = excluded.batting_consistency, bowling_consistency = excluded.bowling_consistency,
          power_hitting = excluded.power_hitting, strike_rotation = excluded.strike_rotation,
          death_overs_batting = excluded.death_overs_batting, death_overs_bowling = excluded.death_overs_bowling,
          spin_effectiveness = excluded.spin_effectiveness, pace_effectiveness = excluded.pace_effectiveness,
          overall_rating = excluded.overall_rating, rating_source = excluded.rating_source,
          method_version = excluded.method_version, computed_at = now();
      end if;
    end if;
  end loop;
  return jsonb_build_object('inserted', n_new, 'updated', n_upd, 'total', n_new + n_upd);
end $$;

-- ════════════════════════════════════════════════════════════════════
--  SECURITY: RLS on, no policies; functions only for service_role
-- ════════════════════════════════════════════════════════════════════
do $$
declare r record;
begin
  for r in select tablename from pg_tables where schemaname = 'public' and tablename like 'ipl\_%' loop
    execute format('alter table public.%I enable row level security', r.tablename);
    execute format('revoke all on table public.%I from anon, authenticated', r.tablename);
  end loop;
  for r in select tablename from pg_tables where schemaname = 'ipl_private' loop
    execute format('alter table ipl_private.%I enable row level security', r.tablename);
    execute format('revoke all on table ipl_private.%I from public, anon, authenticated', r.tablename);
  end loop;
  for r in select p.oid::regprocedure as sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
            where n.nspname = 'public' and p.proname like 'ipl\_%' loop
    execute format('revoke all on function %s from public, anon, authenticated', r.sig);
    execute format('grant execute on function %s to service_role', r.sig);
  end loop;
end $$;
