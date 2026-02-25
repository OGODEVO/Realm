
# NBA Team ID Mappings
# Maps various string representations (Full Name, Abbreviation, Mascot, City) to valid Integer IDs.
# IDs verified against the Rolling Insights team-info endpoint.

TEAM_MAP = {
    # 1 - Minnesota Timberwolves (MIN)
    "minnesota timberwolves": 1, "timberwolves": 1, "wolves": 1, "min": 1, "minnesota": 1,
    # 2 - Indiana Pacers (IND)
    "indiana pacers": 2, "pacers": 2, "ind": 2, "indiana": 2,
    # 3 - Utah Jazz (UTAH)
    "utah jazz": 3, "jazz": 3, "uta": 3, "utah": 3,
    # 4 - Orlando Magic (ORL)
    "orlando magic": 4, "magic": 4, "orl": 4, "orlando": 4,
    # 5 - Atlanta Hawks (ATL)
    "atlanta hawks": 5, "hawks": 5, "atl": 5, "atlanta": 5,
    # 6 - Boston Celtics (BOS)
    "boston celtics": 6, "celtics": 6, "bos": 6, "boston": 6,
    # 7 - Cleveland Cavaliers (CLE)
    "cleveland cavaliers": 7, "cavaliers": 7, "cavs": 7, "cle": 7, "cleveland": 7,
    # 8 - New York Knicks (NY)
    "new york knicks": 8, "knicks": 8, "nyk": 8, "ny": 8, "new york": 8,
    # 9 - New Orleans Pelicans (NO)
    "new orleans pelicans": 9, "pelicans": 9, "nop": 9, "no": 9, "new orleans": 9,
    # 10 - Portland Trail Blazers (POR)
    "portland trail blazers": 10, "trail blazers": 10, "blazers": 10, "por": 10, "portland": 10,
    # 11 - Memphis Grizzlies (MEM)
    "memphis grizzlies": 11, "grizzlies": 11, "mem": 11, "memphis": 11,
    # 12 - Los Angeles Lakers (LAL)
    "los angeles lakers": 12, "lakers": 12, "lal": 12, "l.a. lakers": 12,
    # 13 - Oklahoma City Thunder (OKC)
    "oklahoma city thunder": 13, "thunder": 13, "okc": 13, "oklahoma city": 13,
    # 14 - Dallas Mavericks (DAL)
    "dallas mavericks": 14, "mavericks": 14, "mavs": 14, "dal": 14, "dallas": 14,
    # 15 - Houston Rockets (HOU)
    "houston rockets": 15, "rockets": 15, "hou": 15, "houston": 15,
    # 16 - Denver Nuggets (DEN)
    "denver nuggets": 16, "nuggets": 16, "den": 16, "denver": 16,
    # 17 - Philadelphia 76ers (PHI)
    "philadelphia 76ers": 17, "76ers": 17, "sixers": 17, "phi": 17, "philadelphia": 17,
    # 18 - Brooklyn Nets (BKN)
    "brooklyn nets": 18, "nets": 18, "bkn": 18, "brooklyn": 18,
    # 19 - Sacramento Kings (SAC)
    "sacramento kings": 19, "kings": 19, "sac": 19, "sacramento": 19,
    # 20 - Miami Heat (MIA)
    "miami heat": 20, "heat": 20, "mia": 20, "miami": 20,
    # 21 - Golden State Warriors (GS)
    "golden state warriors": 21, "warriors": 21, "gsw": 21, "gs": 21, "golden state": 21, "dubs": 21,
    # 22 - Chicago Bulls (CHI)
    "chicago bulls": 22, "bulls": 22, "chi": 22, "chicago": 22,
    # 23 - Los Angeles Clippers (LAC)
    "la clippers": 23, "clippers": 23, "lac": 23, "los angeles clippers": 23,
    # 24 - Phoenix Suns (PHX)
    "phoenix suns": 24, "suns": 24, "phx": 24, "phoenix": 24,
    # 25 - Milwaukee Bucks (MIL)
    "milwaukee bucks": 25, "bucks": 25, "mil": 25, "milwaukee": 25,
    # 26 - Detroit Pistons (DET)
    "detroit pistons": 26, "pistons": 26, "det": 26, "detroit": 26,
    # 27 - Charlotte Hornets (CHA)
    "charlotte hornets": 27, "hornets": 27, "cha": 27, "charlotte": 27,
    # 28 - San Antonio Spurs (SA)
    "san antonio spurs": 28, "spurs": 28, "sas": 28, "sa": 28, "san antonio": 28,
    # 29 - Washington Wizards (WSH)
    "washington wizards": 29, "wizards": 29, "was": 29, "wsh": 29, "washington": 29,
    # 30 - Toronto Raptors (TOR)
    "toronto raptors": 30, "raptors": 30, "tor": 30, "toronto": 30,
}

def resolve_team(team_input: str | int) -> int | None:
    """
    Resolves a team input (ID or string name) to the canonical Team ID.
    Returns None if not found.
    """
    if isinstance(team_input, int):
        return team_input
    
    if isinstance(team_input, str):
        # Check if digit string
        if team_input.isdigit():
            return int(team_input)
        
        # Normalize string
        normalized = team_input.lower().strip()
        if normalized in TEAM_MAP:
            return TEAM_MAP[normalized]
    
    return None
