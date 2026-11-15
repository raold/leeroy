"""The fifty US states by full name, with rough 2020 census populations in thousands (the weights
of personas.homes' draw), and their postal abbreviations. Data only: personas.schema checks a US
member's home against these names."""

US_STATES = {
    "Alabama": 5024, "Alaska": 733, "Arizona": 7152, "Arkansas": 3012, "California": 39538,
    "Colorado": 5774, "Connecticut": 3606, "Delaware": 990, "Florida": 21538, "Georgia": 10712,
    "Hawaii": 1455, "Idaho": 1839, "Illinois": 12812, "Indiana": 6786, "Iowa": 3190,
    "Kansas": 2938, "Kentucky": 4506, "Louisiana": 4658, "Maine": 1362, "Maryland": 6177,
    "Massachusetts": 7030, "Michigan": 10077, "Minnesota": 5706, "Mississippi": 2961,
    "Missouri": 6155, "Montana": 1084, "Nebraska": 1962, "Nevada": 3105, "New Hampshire": 1377,
    "New Jersey": 9289, "New Mexico": 2118, "New York": 20201, "North Carolina": 10439,
    "North Dakota": 779, "Ohio": 11799, "Oklahoma": 3959, "Oregon": 4237, "Pennsylvania": 13003,
    "Rhode Island": 1097, "South Carolina": 5119, "South Dakota": 887, "Tennessee": 6911,
    "Texas": 29146, "Utah": 3272, "Vermont": 643, "Virginia": 8631, "Washington": 7705,
    "West Virginia": 1794, "Wisconsin": 5894, "Wyoming": 577,
}

ABBREVIATIONS = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}
