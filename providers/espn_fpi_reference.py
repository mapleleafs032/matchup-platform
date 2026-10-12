"""ESPN FPI reference ranks, transcribed from the published pages.

Purpose: ESPN's API returns each category as an unlabelled array of numbers. Rather than guess which
position holds which column, the ingest job can compare a live pull against these known-correct values
and report which index matches which column. Twenty teams is plenty to identify a column uniquely.

Sources:
  https://www.espn.com/college-football/fpi/_/view/resume/sort/resume.avgsosrank/dir/asc   (SOR, FPI, SOS, REM SOS, GC, AVGWP)
  https://www.espn.com/college-football/fpi/                                               (overall / offense / defense / special teams ranks)

Transcribed 2026-09-17. If ESPN's numbers have moved on, the comparison reports a low match rate for
every index rather than a wrong answer, which is the signal to re-transcribe.
"""

# team -> {column: rank}
CFB_RESUME = {
    "Ohio State Buckeyes":       {"sor": 66, "fpi": 1,  "ap": 6,  "sos": 3,   "rem_sos": 21, "gc": 1,  "avgwp": 18},
    "Texas Longhorns":           {"sor": 1,  "fpi": 2,  "ap": 1,  "sos": 9,   "rem_sos": 12, "gc": 47, "avgwp": 79},
    "Notre Dame Fighting Irish": {"sor": 22, "fpi": 3,  "ap": 3,  "sos": 94,  "rem_sos": 51, "gc": 19, "avgwp": 19},
    "Georgia Bulldogs":          {"sor": 58, "fpi": 4,  "ap": 2,  "sos": 136, "rem_sos": 17, "gc": 4,  "avgwp": 1},
    "Miami Hurricanes":          {"sor": 28, "fpi": 5,  "ap": 5,  "sos": 105, "rem_sos": 47, "gc": 3,  "avgwp": 4},
    "Indiana Hoosiers":          {"sor": 30, "fpi": 6,  "ap": 4,  "sos": 108, "rem_sos": 33, "gc": 10, "avgwp": 6},
    "Alabama Crimson Tide":      {"sor": 6,  "fpi": 7,  "ap": 10, "sos": 62,  "rem_sos": 13, "gc": 14, "avgwp": 41},
    "Texas A&M Aggies":          {"sor": 17, "fpi": 8,  "ap": 9,  "sos": 86,  "rem_sos": 7,  "gc": 39, "avgwp": 31},
    "LSU Tigers":                {"sor": 13, "fpi": 9,  "ap": 7,  "sos": 76,  "rem_sos": 9,  "gc": 13, "avgwp": 21},
    "Tennessee Volunteers":      {"sor": 12, "fpi": 10, "ap": 15, "sos": 74,  "rem_sos": 14, "gc": 6,  "avgwp": 20},
    "Oklahoma Sooners":          {"sor": 70, "fpi": 11, "ap": 24, "sos": 31,  "rem_sos": 2,  "gc": 37, "avgwp": 62},
    "USC Trojans":               {"sor": 19, "fpi": 12, "ap": 12, "sos": 102, "rem_sos": 16, "gc": 11, "avgwp": 14},
    "Oregon Ducks":              {"sor": 42, "fpi": 13, "ap": 21, "sos": 43,  "rem_sos": 29, "gc": 88, "avgwp": 115},
    "Penn State Nittany Lions":  {"sor": 29, "fpi": 14, "ap": 14, "sos": 106, "rem_sos": 63, "gc": 15, "avgwp": 25},
    "Florida Gators":            {"sor": 53, "fpi": 15, "ap": None, "sos": 133, "rem_sos": 6, "gc": 25, "avgwp": 8},
    "Texas Tech Red Raiders":    {"sor": 23, "fpi": 16, "ap": 13, "sos": 97,  "rem_sos": 66, "gc": 42, "avgwp": 39},
    "Ole Miss Rebels":           {"sor": 5,  "fpi": 17, "ap": 8,  "sos": 56,  "rem_sos": 3,  "gc": 32, "avgwp": 32},
    "BYU Cougars":               {"sor": 15, "fpi": 18, "ap": 11, "sos": 83,  "rem_sos": 40, "gc": 55, "avgwp": 36},
    "Utah Utes":                 {"sor": 36, "fpi": 19, "ap": 17, "sos": 119, "rem_sos": 59, "gc": 17, "avgwp": 37},
    "Nebraska Cornhuskers":      {"sor": 48, "fpi": 20, "ap": None, "sos": 128, "rem_sos": 28, "gc": 83, "avgwp": 37},
}

CFB_EFFICIENCY = {
    "Notre Dame Fighting Irish":  {"overall": 1,  "offense": 12, "defense": 4,  "special_teams": 28},
    "Miami Hurricanes":           {"overall": 2,  "offense": 1,  "defense": 16, "special_teams": 42},
    "Indiana Hoosiers":           {"overall": 3,  "offense": 3,  "defense": 24, "special_teams": 24},
    "Texas Longhorns":            {"overall": 4,  "offense": 9,  "defense": 13, "special_teams": 17},
    "Georgia Bulldogs":           {"overall": 5,  "offense": 2,  "defense": 29, "special_teams": 23},
    "Ohio State Buckeyes":        {"overall": 6,  "offense": 6,  "defense": 8,  "special_teams": 123},
    "Alabama Crimson Tide":       {"overall": 7,  "offense": 32, "defense": 3,  "special_teams": 32},
    "Kansas State Wildcats":      {"overall": 8,  "offense": 28, "defense": 7,  "special_teams": 10},
    "Florida Gators":             {"overall": 9,  "offense": 11, "defense": 41, "special_teams": 3},
    "LSU Tigers":                 {"overall": 10, "offense": 49, "defense": 1,  "special_teams": 72},
    "Utah Utes":                  {"overall": 11, "offense": 8,  "defense": 28, "special_teams": 43},
    "Texas A&M Aggies":           {"overall": 12, "offense": 61, "defense": 2,  "special_teams": 20},
    "SMU Mustangs":               {"overall": 13, "offense": 4,  "defense": 22, "special_teams": 119},
    "Penn State Nittany Lions":   {"overall": 14, "offense": 20, "defense": 21, "special_teams": 19},
    "Oklahoma Sooners":           {"overall": 15, "offense": 47, "defense": 14, "special_teams": 5},
    "Mississippi State Bulldogs": {"overall": 16, "offense": 5,  "defense": 44, "special_teams": 88},
    "BYU Cougars":                {"overall": 17, "offense": 10, "defense": 48, "special_teams": 34},
    "Virginia Cavaliers":         {"overall": 18, "offense": 39, "defense": 6,  "special_teams": 92},
    "Virginia Tech Hokies":       {"overall": 19, "offense": 37, "defense": 20, "special_teams": 15},
    "Tennessee Volunteers":       {"overall": 20, "offense": 19, "defense": 17, "special_teams": 99},
}

REFERENCE = {"CFB": {"resume": CFB_RESUME, "efficiencies": CFB_EFFICIENCY}}


# NFL. Note this view publishes OFF / DEF / ST as EFFICIENCY VALUES, not ranks, while FPI's RK column
# and SOS / REM SOS / AVGWP are ranks. Both kinds are recorded: the values identify which array slot
# holds each efficiency, and the rank paired with it is the one the table shows.
# Source: https://www.espn.com/nfl/fpi/_/sort/fpi.epaoffense/dir/desc   Transcribed 2026-09-21.
NFL_RANKS = {
    "Buffalo Bills":         {"fpi": 2,  "sos": 10, "rem_sos": 26, "avgwp": 8},
    "San Francisco 49ers":   {"fpi": 1,  "sos": 7,  "rem_sos": 21, "avgwp": 9},
    "Baltimore Ravens":      {"fpi": 3,  "sos": 24, "rem_sos": 29, "avgwp": 2},
    "Detroit Lions":         {"fpi": 9,  "sos": 12, "rem_sos": 30, "avgwp": 17},
    "Dallas Cowboys":        {"fpi": 8,  "sos": 20, "rem_sos": 4,  "avgwp": 27},
    "Chicago Bears":         {"fpi": 6,  "sos": 26, "rem_sos": 8,  "avgwp": 10},
    "Cincinnati Bengals":    {"fpi": 11, "sos": 19, "rem_sos": 32, "avgwp": 3},
    "Kansas City Chiefs":    {"fpi": 4,  "sos": 23, "rem_sos": 27, "avgwp": 7},
    "Jacksonville Jaguars":  {"fpi": 5,  "sos": 31, "rem_sos": 17, "avgwp": 1},
    "Green Bay Packers":     {"fpi": 14, "sos": 16, "rem_sos": 10, "avgwp": 14},
    "Los Angeles Rams":      {"fpi": 7,  "sos": 1,  "rem_sos": 3,  "avgwp": 24},
    "New York Giants":       {"fpi": 17, "sos": 11, "rem_sos": 14, "avgwp": 6},
    "New England Patriots":  {"fpi": 13, "sos": 6,  "rem_sos": 18, "avgwp": 15},
    "Washington Commanders": {"fpi": 18, "sos": 14, "rem_sos": 6,  "avgwp": 21},
    "Philadelphia Eagles":   {"fpi": 12, "sos": 21, "rem_sos": 9,  "avgwp": 12},
    "Tampa Bay Buccaneers":  {"fpi": 16, "sos": 13, "rem_sos": 23, "avgwp": 30},
    "Houston Texans":        {"fpi": 10, "sos": 2,  "rem_sos": 24, "avgwp": 16},
    "Indianapolis Colts":    {"fpi": 23, "sos": 3,  "rem_sos": 22, "avgwp": 31},
    "Los Angeles Chargers":  {"fpi": 15, "sos": 27, "rem_sos": 5,  "avgwp": 22},
    "Arizona Cardinals":     {"fpi": 24, "sos": 18, "rem_sos": 2,  "avgwp": 11},
    "New Orleans Saints":    {"fpi": 21, "sos": 9,  "rem_sos": 31, "avgwp": 25},
    "Carolina Panthers":     {"fpi": 26, "sos": 8,  "rem_sos": 19, "avgwp": 23},
}

NFL_EFFICIENCY_VALUES = {
    "Buffalo Bills":         {"fpi_value": 5.6, "offense": 6.3,  "defense": -0.7, "special_teams": 0.1},
    "San Francisco 49ers":   {"fpi_value": 5.6, "offense": 4.4,  "defense": 0.9,  "special_teams": 0.4},
    "Baltimore Ravens":      {"fpi_value": 4.8, "offense": 3.6,  "defense": 1.2,  "special_teams": 0.0},
    "Detroit Lions":         {"fpi_value": 2.0, "offense": 3.2,  "defense": -1.2, "special_teams": 0.0},
    "Dallas Cowboys":        {"fpi_value": 2.0, "offense": 2.8,  "defense": -1.0, "special_teams": 0.2},
    "Chicago Bears":         {"fpi_value": 2.9, "offense": 2.5,  "defense": 0.1,  "special_teams": 0.3},
    "Cincinnati Bengals":    {"fpi_value": 1.6, "offense": 2.3,  "defense": -0.6, "special_teams": -0.1},
    "Kansas City Chiefs":    {"fpi_value": 4.3, "offense": 2.2,  "defense": 2.2,  "special_teams": -0.1},
    "Jacksonville Jaguars":  {"fpi_value": 3.5, "offense": 2.0,  "defense": 1.4,  "special_teams": 0.1},
    "Green Bay Packers":     {"fpi_value": 1.1, "offense": 1.7,  "defense": -0.5, "special_teams": -0.1},
    "Los Angeles Rams":      {"fpi_value": 2.6, "offense": 1.7,  "defense": 1.0,  "special_teams": -0.1},
    "New York Giants":       {"fpi_value": -0.2, "offense": 1.3, "defense": -1.4, "special_teams": -0.1},
    "New England Patriots":  {"fpi_value": 1.4, "offense": 0.5,  "defense": 1.0,  "special_teams": 0.0},
    "Washington Commanders": {"fpi_value": -0.7, "offense": 0.3, "defense": -1.0, "special_teams": 0.0},
    "Philadelphia Eagles":   {"fpi_value": 1.5, "offense": 0.3,  "defense": 1.4,  "special_teams": -0.2},
    "Tampa Bay Buccaneers":  {"fpi_value": 0.0, "offense": 0.2,  "defense": -0.5, "special_teams": 0.3},
    "Houston Texans":        {"fpi_value": 1.8, "offense": 0.1,  "defense": 1.6,  "special_teams": 0.2},
    "Indianapolis Colts":    {"fpi_value": -1.9, "offense": -0.8, "defense": -1.1, "special_teams": 0.0},
    "Los Angeles Chargers":  {"fpi_value": 0.4, "offense": -0.9, "defense": 1.0,  "special_teams": 0.2},
    "Arizona Cardinals":     {"fpi_value": -3.0, "offense": -1.2, "defense": -1.8, "special_teams": 0.0},
    "New Orleans Saints":    {"fpi_value": -1.5, "offense": -1.3, "defense": -0.1, "special_teams": -0.1},
    "Carolina Panthers":     {"fpi_value": -3.2, "offense": -1.3, "defense": -1.6, "special_teams": -0.2},
}

REFERENCE["NFL"] = {"resume": NFL_RANKS, "efficiencies": NFL_EFFICIENCY_VALUES}
