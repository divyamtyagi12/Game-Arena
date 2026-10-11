"""Migration script: Update player eligibility to 2020-2026 IPL squads using fast bulk updates."""
import asyncio
from config import load_settings
from database.client import Database

SEED_SEASONS = {
    "rohit-sharma": (2008, 2026, True),
    "rinku-singh": (2017, 2026, True),
    "sai-sudharsan": (2022, 2026, True),
    "shahrukh-khan": (2021, 2026, True),
    "sarfaraz-khan": (2015, 2026, True),
    "dinesh-karthik": (2008, 2024, True),
    "varun-chakravarthy": (2019, 2026, True),
    "prasidh-krishna": (2018, 2026, True),
    "vinay-kumar": (2008, 2018, False),
    "sreesanth": (2008, 2013, False),
    "rahul-sharma": (2010, 2014, False),
    "vyshak-vijaykumar": (2023, 2026, True),
    "sai-kishore": (2020, 2026, True),
    "nathan-coulter-nile": (2013, 2022, True),
    "rassie-van-der-dussen": (2022, 2022, True),
    "reeza-hendricks": (2024, 2024, True),
    "albie-morkel": (2008, 2016, False),
    "ross-taylor": (2008, 2014, False),
    "finn-allen": (2021, 2023, True),
    "fabian-allen": (2020, 2022, True),
    "chadwick-walton": (2018, 2018, False),
    "lasith-malinga": (2009, 2019, False),
    "kusal-perera": (2013, 2021, True),
    "mahela-jayawardene": (2008, 2014, False),
    "thisara-perera": (2010, 2016, False),
    "wanindu-hasaranga": (2021, 2024, True),
    "dasun-shanaka": (2023, 2023, True),
    "kamindu-mendis": (2025, 2026, True),
    "dushmantha-chameera": (2018, 2024, True),
    "nuwan-kulasekara": (2009, 2012, False),
    "ajantha-mendis": (2008, 2013, False),
    "abdul-razzaq": (2011, 2011, False),
    "ryan-ten-doeschate": (2011, 2015, False),
}

async def run():
    s = load_settings()
    db = Database(s.supabase_url, s.supabase_key)
    
    print("1. Updating seed players seasons and eligibility...")
    for slug, (fs, ls, el) in SEED_SEASONS.items():
        is_26 = (ls == 2026)
        await db.exec(lambda c, sl=slug, fs=fs, ls=ls, el=is_26: c.table("ipl_players").update({
            "first_ipl_season": fs,
            "last_ipl_season": ls,
            "is_eligible": el,
        }).eq("slug", sl))
    print(f"Updated {len(SEED_SEASONS)} seed players.")

    print("2. Bulk updating last_ipl_season < 2026 to is_eligible = False...")
    res = await db.exec(lambda c: c.table("ipl_players").update({"is_eligible": False}).lt("last_ipl_season", 2026))
    print(f"Bulk updated older players: {len(res) if res else 'OK'}")

    eligible = await db.exec(lambda c: c.table("ipl_players").select("id, display_name, last_ipl_season").eq("is_eligible", True))
    ineligible = await db.exec(lambda c: c.table("ipl_players").select("id").eq("is_eligible", False))
    print(f"SUCCESS: Eligible={len(eligible)}, Ineligible={len(ineligible)}")

if __name__ == "__main__":
    asyncio.run(run())
