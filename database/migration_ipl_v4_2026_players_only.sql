-- ══════════════════════════════════════════════════════════════════════
-- IPL DRAFT MIGRATION V4: 2026 IPL SQUAD PLAYERS ONLY
-- 
-- 1. Clears completed/cancelled test tournament history so foreign keys are unlocked.
-- 2. Sets all 2026 IPL squad players to is_eligible = true and last_ipl_season = 2026.
-- 3. Deletes all historical/pre-2026 IPL players from ipl_players (keeping ONLY 2026 squad members).
-- ══════════════════════════════════════════════════════════════════════

begin;

-- 1. Clear tournament records and sub-tables (matches, rosters, offers, pool)
delete from ipl_batting_scorecards;
delete from ipl_bowling_scorecards;
delete from ipl_ball_events;
delete from ipl_innings;
delete from ipl_matches;
delete from ipl_fixtures;
delete from ipl_standings;
delete from ipl_player_tournament_stats;
delete from ipl_team_awards;
delete from ipl_tournament_awards;
delete from ipl_team_rosters;
delete from ipl_draft_picks;
delete from ipl_draft_offer_candidates;
delete from ipl_draft_offers;
delete from ipl_tournament_pool;
delete from ipl_tournament_teams;
delete from ipl_tournaments;

-- 2. Ensure all 2026 squad players are marked as 2026 season & eligible
update ipl_players
   set is_eligible = true,
       last_ipl_season = 2026
 where last_ipl_season = 2026
    or lower(display_name) in (
       -- CSK
       'ms dhoni', 'sanju samson', 'ruturaj gaikwad', 'ayush mhatre', 'dewald brevis', 'urvil patel', 'kartik sharma',
       'matthew short', 'sarfaraz khan', 'anshul kamboj', 'gurjapneet singh', 'mukesh choudhary', 'nathan ellis',
       'noor ahmad', 'khaleel ahmed', 'matt henry', 'rahul chahar', 'zak foulkes', 'jamie overton',
       'ramakrishna ghosh', 'shivam dube', 'shreyas gopal', 'akeal hosein', 'prashant veer', 'aman khan',
       'spencer johnson', 'kuldip yadav', 'akash madhwal', 'macneil noronha', 'dian forrester',
       -- DC
       'abishek porel', 'abhishek porel', 'ashutosh sharma', 'karun nair', 'kl rahul', 'nitish rana', 'sameer rizvi', 'tristan stubbs',
       'david miller', 'ben duckett', 'pathum nissanka', 'sahil parakh', 'prithvi shaw', 'dushmantha chameera',
       'kuldeep yadav', 'mitchell starc', 'mukesh kumar', 't. natarajan', 't natarajan', 'tripurana vijay', 'auqib nabi',
       'kyle jamieson', 'ajay mandal', 'axar patel', 'madhav tiwari', 'vipraj nigam', 'rehan ahmed', 'lungi ngidi',
       -- GT
       'anuj rawat', 'jos buttler', 'kumar kushagra', 'sai sudharsan', 'shubman gill', 'tom banton', 'gurnoor brar',
       'ishant sharma', 'kagiso rabada', 'mohammed siraj', 'arshad khan', 'prasidh krishna', 'sai kishore',
       'rashid khan', 'ashok sharma', 'prithvi raj yarra', 'luke wood', 'glenn phillips', 'jayant yadav',
       'manav suthar', 'nishant sindhu', 'rahul tewatia', 'shahrukh khan', 'washington sundar', 'jason holder',
       'kulwant khejroliya', 'connor esterhuizen',
       -- KKR
       'ajinkya rahane', 'angkrish raghuvanshi', 'manish pandey', 'rinku singh', 'rovman powell', 'finn allen',
       'tejasvi singh', 'rahul tripathi', 'tim seifert', 'harshit rana', 'umran malik', 'vaibhav arora',
       'varun chakravarthy', 'matheesha pathirana', 'kartik tyagi', 'prashant solanki', 'mustafizur rahman',
       'akash deep', 'anukul roy', 'ramandeep singh', 'sunil narine', 'cameron green', 'sarthak ranjan',
       'daksh kamra', 'rachin ravindra', 'blessing muzarabani', 'navdeep saini', 'saurabh dubey', 'luvnith sisodia',
       -- LSG
       'abdul samad', 'himmat singh', 'matthew breetzke', 'nicholas pooran', 'rishabh pant', 'mukul choudhary',
       'akshat raghuwanshi', 'josh inglis', 'akash singh', 'arjun tendulkar', 'avesh khan', 'digvesh rathi',
       'mamimaran siddharth', 'manimaran siddharth', 'mayank yadav', 'mohammed shami', 'mohsin khan', 'prince yadav', 'anrich nortje',
       'naman tiwari', 'aiden markram', 'arshin kulkarni', 'ayush badoni', 'mitchell marsh', 'shahbaz ahmed',
       'wanindu hasaranga', 'george linde',
       -- MI
       'robin minz', 'rohit sharma', 'ryan rickelton', 'sherfane rutherford', 'suryakumar yadav', 'tilak varma',
       'quinton de kock', 'danish malewar', 'allah ghazanfar', 'ashwani kumar', 'deepak chahar', 'jasprit bumrah',
       'mayank markande', 'raghu sharma', 'shardul thakur', 'trent boult', 'mohammad izhar', 'corbin bosch',
       'hardik pandya', 'mitchell santner', 'naman dhir', 'raj angad bawa', 'will jacks', 'atharva ankolekar',
       'mayank rawat', 'krish bhagat', 'keshav maharaj', 'mahipal lomror', 'ruchit ahir',
       -- PBKS
       'harnoor singh pannu', 'nehal wadhera', 'prabhsimran singh', 'priyansh arya', 'pyla avinash', 'shashank singh',
       'shreyas iyer', 'vishnu vinod', 'arshdeep singh', 'lockie ferguson', 'vyshak vijaykumar', 'xavier bartlett',
       'yash thakur', 'yuzvendra chahal', 'ben dwarshuis', 'azmatullah omarzai', 'harpreet brar', 'marco jansen',
       'marcus stoinis', 'mitchell owen', 'musheer khan', 'suryansh shedge', 'cooper connolly', 'pravin dubey',
       'vishal nishad',
       -- RR
       'dhruv jurel', 'lhuan-dre pretorius', 'shimron hetmyer', 'shubham dubey', 'vaibhav suryavanshi',
       'yashasvi jaiswal', 'ravi singh', 'aman rao', 'jofra archer', 'kwena maphaka', 'nandre burger',
       'sandeep sharma', 'tushar deshpande', 'yudhvir singh charak', 'ravi bishnoi', 'sushant mishra',
       'yash raj punja', 'vignesh puthur', 'brijesh sharma', 'adam milne', 'kuldeep sen', 'donovan ferreira',
       'ravindra jadeja', 'riyan parag', 'sam curran', 'dasun shanaka', 'emanjot chahal',
       -- RCB
       'devdutt padikkal', 'jitesh sharma', 'phil salt', 'rajat patidar', 'tim david', 'virat kohli', 'jordan cox',
       'vihaan malhotra', 'abhinandan singh', 'bhuvneshwar kumar', 'josh hazlewood', 'nuwan thushara', 'rasikh dar',
       'suyash sharma', 'yash dayal', 'jacob duffy', 'vicky ostwal', 'jacob bethell', 'krunal pandya',
       'romario shepherd', 'swapnil singh', 'venkatesh iyer', 'satvik deswal', 'mangesh yadav', 'kanishk chouhan',
       'richard gleeson',
       -- SRH
       'aniket verma', 'heinrich klaasen', 'ishan kishan', 'smaran ravichandran', 'travis head', 'salil arora',
       'brydon carse', 'eshan malinga', 'harshal patel', 'jaydev unadkat', 'pat cummins', 'zeeshan ansari',
       'sakib hassan', 'onkar tarmale', 'amit kumar', 'praful hinge', 'abhishek sharma', 'harsh dubey',
       'kamindu mendis', 'nitish kumar reddy', 'shivang kumar', 'krains fuletra', 'liam livingstone',
       'shivam mavi', 'jack edwards', 'david payne', 'dilshan madushanka', 'gerald coetzee', 'rs ambrish'
    );

-- 3. Delete all non-2026 players from ipl_players (cascade removes career/season stats and private ratings)
delete from ipl_players
 where last_ipl_season < 2026
    or last_ipl_season is null
    or not is_eligible;

commit;

-- Summary query to verify remaining count
select count(*) as total_2026_players,
       count(*) filter (where is_eligible) as eligible_players,
       count(distinct role) as distinct_roles
  from ipl_players;
