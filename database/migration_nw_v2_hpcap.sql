-- ══════════════════════════════════════════════════════════════════════
-- NUMBER WARS MIGRATION V2 — HP CAP 5 IN NW_APPLY_ROUND
-- Fixes clamp mismatch: least(8, ...) -> least(5, ...)
-- Safe to re-run (idempotent).
-- ══════════════════════════════════════════════════════════════════════

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

revoke all on function nw_apply_round(bigint, numeric, jsonb, boolean, bigint[], text) from public, anon, authenticated;
grant execute on function nw_apply_round(bigint, numeric, jsonb, boolean, bigint[], text) to service_role;
