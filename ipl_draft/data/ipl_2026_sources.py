"""IPL 2026 squad EVIDENCE (names only). Nothing here is read by the game at runtime — it exists so that
scripts/verify_ipl_2026.py can compare the player pool against external records and report the result.

Retrieved 2026-10-10/11 through a web-fetch tool (the sandbox shell could not reach iplt20.com or cricsheet.org; the
fetched iplt20.com squad page was truncated to 4 players, so it could NOT be used). Therefore:

    OFFICIAL (iplt20.com) verification status: NOT VERIFIED.   These lists are SECONDARY sources.

SOURCES
  WISDEN   https://www.wisden.com/series/ipl-2026/cricket-news/ipl-2026-squads-complete-team-lists-after-the-indian-premier-league-mini-auction
           published 2025-12-16, post-mini-auction squads, all 10 teams (no in-season changes).
  WIKI_<T> https://en.wikipedia.org/wiki/2026_<Team>_season  squad tables incl. replacements (9 teams; LSG page has no table)
  WIKI_CHG https://en.wikipedia.org/wiki/List_of_2026_Indian_Premier_League_personnel_changes   trades + injury replacements
  CRIC_LSG https://www.crictracker.com/cricket-news/lsg-squad-2026-player-list-complete-lucknow-super-giants-squad-and-players-list-for-ipl-2026/
"""
from __future__ import annotations

WISDEN_SQUADS: dict[str, list[str]] = {
    "CSK": ["MS Dhoni", "Sanju Samson", "Ruturaj Gaikwad", "Ayush Mhatre", "Dewald Brevis", "Urvil Patel", "Kartik Sharma",
            "Matthew Short", "Sarfaraz Khan", "Anshul Kamboj", "Gurjapneet Singh", "Mukesh Choudhary", "Nathan Ellis",
            "Noor Ahmad", "Khaleel Ahmed", "Matt Henry", "Rahul Chahar", "Zak Foulkes", "Jamie Overton",
            "Ramakrishna Ghosh", "Shivam Dube", "Shreyas Gopal", "Akeal Hosein", "Prashant Veer", "Aman Khan"],
    "DC": ["Abishek Porel", "Ashutosh Sharma", "Karun Nair", "KL Rahul", "Nitish Rana", "Sameer Rizvi", "Tristan Stubbs",
           "David Miller", "Ben Duckett", "Pathum Nissanka", "Sahil Parakh", "Prithvi Shaw", "Dushmantha Chameera",
           "Kuldeep Yadav", "Mitchell Starc", "Mukesh Kumar", "T. Natarajan", "Tripurana Vijay", "Auqib Nabi",
           "Kyle Jamieson", "Ajay Mandal", "Axar Patel", "Madhav Tiwari", "Vipraj Nigam"],
    "GT": ["Anuj Rawat", "Jos Buttler", "Kumar Kushagra", "Sai Sudharsan", "Shubman Gill", "Tom Banton", "Gurnoor Brar",
           "Ishant Sharma", "Kagiso Rabada", "Mohammed Siraj", "Arshad Khan", "Prasidh Krishna", "Sai Kishore",
           "Rashid Khan", "Ashok Sharma", "Prithvi Raj Yarra", "Luke Wood", "Glenn Phillips", "Jayant Yadav",
           "Manav Suthar", "Nishant Sindhu", "Rahul Tewatia", "Shahrukh Khan", "Washington Sundar", "Jason Holder"],
    "KKR": ["Ajinkya Rahane", "Angkrish Raghuvanshi", "Manish Pandey", "Rinku Singh", "Rovman Powell", "Finn Allen",
            "Tejasvi Singh", "Rahul Tripathi", "Tim Seifert", "Harshit Rana", "Umran Malik", "Vaibhav Arora",
            "Varun Chakravarthy", "Matheesha Pathirana", "Kartik Tyagi", "Prashant Solanki", "Mustafizur Rahman",
            "Akash Deep", "Anukul Roy", "Ramandeep Singh", "Sunil Narine", "Cameron Green", "Sarthak Ranjan",
            "Daksh Kamra", "Rachin Ravindra"],
    "LSG": ["Abdul Samad", "Himmat Singh", "Matthew Breetzke", "Nicholas Pooran", "Rishabh Pant", "Mukul Choudhary",
            "Akshat Raghuwanshi", "Josh Inglis", "Akash Singh", "Arjun Tendulkar", "Avesh Khan", "Digvesh Rathi",
            "Mamimaran Siddharth", "Mayank Yadav", "Mohammed Shami", "Mohsin Khan", "Prince Yadav", "Anrich Nortje",
            "Naman Tiwari", "Aiden Markram", "Arshin Kulkarni", "Ayush Badoni", "Mitchell Marsh", "Shahbaz Ahmed",
            "Wanindu Hasaranga"],
    "MI": ["Robin Minz", "Rohit Sharma", "Ryan Rickelton", "Sherfane Rutherford", "Suryakumar Yadav", "Tilak Varma",
           "Quinton de Kock", "Danish Malewar", "Allah Ghazanfar", "Ashwani Kumar", "Deepak Chahar", "Jasprit Bumrah",
           "Mayank Markande", "Raghu Sharma", "Shardul Thakur", "Trent Boult", "Mohammad Izhar", "Corbin Bosch",
           "Hardik Pandya", "Mitchell Santner", "Naman Dhir", "Raj Angad Bawa", "Will Jacks", "Atharva Ankolekar",
           "Mayank Rawat"],
    "PBKS": ["Harnoor Singh Pannu", "Nehal Wadhera", "Prabhsimran Singh", "Priyansh Arya", "Pyla Avinash", "Shashank Singh",
             "Shreyas Iyer", "Vishnu Vinod", "Arshdeep Singh", "Lockie Ferguson", "Vyshak Vijaykumar", "Xavier Bartlett",
             "Yash Thakur", "Yuzvendra Chahal", "Ben Dwarshuis", "Azmatullah Omarzai", "Harpreet Brar", "Marco Jansen",
             "Marcus Stoinis", "Mitchell Owen", "Musheer Khan", "Suryansh Shedge", "Cooper Connolly", "Pravin Dubey",
             "Vishal Nishad"],
    "RR": ["Dhruv Jurel", "Lhuan-dre Pretorius", "Shimron Hetmyer", "Shubham Dubey", "Vaibhav Suryavanshi",
           "Yashasvi Jaiswal", "Ravi Singh", "Aman Rao", "Jofra Archer", "Kwena Maphaka", "Nandre Burger",
           "Sandeep Sharma", "Tushar Deshpande", "Yudhvir Singh Charak", "Ravi Bishnoi", "Sushant Mishra",
           "Yash Raj Punja", "Vignesh Puthur", "Brijesh Sharma", "Adam Milne", "Kuldeep Sen", "Donovan Ferreira",
           "Ravindra Jadeja", "Riyan Parag", "Sam Curran"],
    "RCB": ["Devdutt Padikkal", "Jitesh Sharma", "Phil Salt", "Rajat Patidar", "Tim David", "Virat Kohli", "Jordan Cox",
            "Vihaan Malhotra", "Abhinandan Singh", "Bhuvneshwar Kumar", "Josh Hazlewood", "Nuwan Thushara", "Rasikh Dar",
            "Suyash Sharma", "Yash Dayal", "Jacob Duffy", "Vicky Ostwal", "Jacob Bethell", "Krunal Pandya",
            "Romario Shepherd", "Swapnil Singh", "Venkatesh Iyer", "Satvik Deswal", "Mangesh Yadav", "Kanishk Chouhan"],
    "SRH": ["Aniket Verma", "Heinrich Klaasen", "Ishan Kishan", "Smaran Ravichandran", "Travis Head", "Salil Arora",
            "Brydon Carse", "Eshan Malinga", "Harshal Patel", "Jaydev Unadkat", "Pat Cummins", "Zeeshan Ansari",
            "Sakib Hassan", "Onkar Tarmale", "Amit Kumar", "Praful Hinge", "Abhishek Sharma", "Harsh Dubey",
            "Kamindu Mendis", "Nitish Kumar Reddy", "Shivang Kumar", "Krains Fuletra", "Liam Livingstone",
            "Shivam Mavi", "Jack Edwards"],
}

# In-season / injury replacements joined during the 2026 season (WIKI_CHG; also visible on the WIKI_<team> pages)
REPLACEMENTS_IN: dict[str, list[str]] = {
    "KKR": ["Blessing Muzarabani", "Navdeep Saini", "Saurabh Dubey", "Luvnith Sisodia"],
    "MI": ["Krish Bhagat", "Keshav Maharaj", "Mahipal Lomror", "Ruchit Ahir"],
    "CSK": ["Spencer Johnson", "Kuldip Yadav", "Akash Madhwal", "Macneil Noronha", "Dian Forrester"],
    "RR": ["Dasun Shanaka", "Emanjot Chahal"],
    "SRH": ["David Payne", "Dilshan Madushanka", "Gerald Coetzee", "RS Ambrish"],
    "DC": ["Rehan Ahmed"],
    "GT": ["Kulwant Khejroliya", "Connor Esterhuizen"],
    "LSG": ["George Linde"],
    "RCB": ["Richard Gleeson"],
}
# Players who appear ONLY on a secondary source (WIKI team page) but not on Wisden's post-auction list
EXTRA_ON_WIKI: dict[str, list[str]] = {
    "DC": ["Lungi Ngidi"],       # WIKI_DC lists him; not in Wisden's Dec-2025 list; not confirmed on iplt20.com -> flagged
    "LSG": ["David Miller"],     # CRIC_LSG lists Miller under LSG while Wisden lists him under DC -> CONFLICT, flagged
}
SOURCE_CONFLICTS = [
    "David Miller: Wisden + WIKI_DC list him at Delhi Capitals; CRIC_LSG lists him at Lucknow Super Giants.",
    "Lungi Ngidi: WIKI_DC lists him; Wisden (Dec 2025) does not. Not confirmable from the official site in this session.",
    "Pant/Kuldeep Yadav: an iplt20.com headline mentions a DC<->LSG trade; Wisden (Dec 2025) still lists Pant at LSG. "
    "LSG's final squad could not be confirmed from a structured source.",
    "Spelling variants across sources: Vaibhav Suryavanshi/Sooryavanshi, Shahbaz Ahmed/Ahmad, Prithvi Raj/Prithvi Raj Yarra, "
    "Rasikh Dar/Rasikh Salam, Sakib Hassan/Hussain, Praful Hinge, Vyshak Vijaykumar/Vijaykumar Vyshak.",
]
