"""Static reference data for the synthetic warehouse.

All merchant names, banks and people are fictional.
"""
from __future__ import annotations

SUBS = [
    "UPI_INTENT", "UPI_COLLECT", "UPI_QR", "CREDIT", "DEBIT",
    "PREPAID", "NETBANKING", "WALLET", "EMI", "BNPL",
]
SUB_INDEX = {s: i for i, s in enumerate(SUBS)}

SUB_TO_METHOD = {
    "UPI_INTENT": "UPI", "UPI_COLLECT": "UPI", "UPI_QR": "UPI",
    "CREDIT": "CARD", "DEBIT": "CARD", "PREPAID": "CARD",
    "NETBANKING": "NETBANKING", "WALLET": "WALLET", "EMI": "EMI", "BNPL": "BNPL",
}
# Value written to dim_payment_method.sub_method
SUB_METHOD_LABEL = {s: ("CARD_EMI" if s == "EMI" else s) for s in SUBS}
CARD_LIKE_SUBS = {"CREDIT", "DEBIT", "PREPAID", "EMI"}

NETWORKS = ["VISA", "MASTERCARD", "RUPAY", "AMEX"]
NETWORK_MIX = {
    "CREDIT": [0.45, 0.30, 0.15, 0.10],
    "DEBIT": [0.35, 0.20, 0.45, 0.0],
    "PREPAID": [1.0, 0.0, 0.0, 0.0],
    "EMI": [0.50, 0.35, 0.15, 0.0],
}

CHANNELS = ["APP", "WEB", "POS", "LINK"]
DEVICES = ["ANDROID", "IOS", "WEB", "POS"]
INTEGRATIONS = ["POS", "ONLINE_PG", "PAYMENT_LINK", "SDK"]

# code, description, category, retryable, customer_attributable
FAILURE_REASONS = [
    ("05", "Do not honour", "ISSUER", False, False),
    ("51", "Insufficient funds", "CUSTOMER", False, True),
    ("54", "Expired card", "CUSTOMER", False, True),
    ("55", "Incorrect PIN", "CUSTOMER", True, True),
    ("59", "Suspected fraud", "RISK_DECLINE", False, False),
    ("91", "Issuer or switch inoperative", "ISSUER", True, False),
    ("96", "System malfunction", "NETWORK", True, False),
    ("3DS_FAIL", "3-D Secure authentication failed", "CUSTOMER", True, True),
    ("ACQ_TO", "Acquirer response timeout", "ACQUIRER", True, False),
    ("U30", "Debit failed at remitter bank", "ISSUER", True, False),
    ("U69", "Collect request expired", "USER_DROP", True, True),
    ("ZM", "Invalid UPI PIN", "CUSTOMER", True, True),
    ("Z9", "Insufficient funds in account", "CUSTOMER", False, True),
    ("U16", "Risk threshold exceeded", "RISK_DECLINE", False, False),
    ("U28", "PSP not available", "NETWORK", True, False),
    ("XY", "Remitter bank core banking offline", "ISSUER", True, False),
    ("BANK_TO", "Remitter bank timeout", "ISSUER", True, False),
    ("NB_DROP", "User abandoned at bank page", "USER_DROP", True, True),
    ("NB_AUTH", "Net banking authentication failed", "CUSTOMER", True, True),
    ("NB_TECH", "Bank technical error", "ISSUER", True, False),
    ("W_BAL", "Insufficient wallet balance", "CUSTOMER", False, True),
    ("W_LIMIT", "Wallet limit exceeded", "CUSTOMER", False, True),
    ("EMI_INELIGIBLE", "Card not eligible for EMI", "CUSTOMER", False, True),
    ("BNPL_DECLINED", "BNPL credit check declined", "RISK_DECLINE", False, False),
    ("GW_ERR", "Payment gateway internal error", "GATEWAY_TECHNICAL", True, False),
    ("GW_TO", "Payment gateway timeout", "GATEWAY_TECHNICAL", True, False),
    ("UC", "User cancelled payment", "USER_DROP", True, True),
]

FAILURE_GROUPS = ["CARD", "EMI", "UPI", "UPI_COLLECT", "NETBANKING", "WALLET", "BNPL"]
SUB_GROUP = {
    "UPI_INTENT": "UPI", "UPI_QR": "UPI", "UPI_COLLECT": "UPI_COLLECT",
    "CREDIT": "CARD", "DEBIT": "CARD", "PREPAID": "CARD", "EMI": "EMI",
    "NETBANKING": "NETBANKING", "WALLET": "WALLET", "BNPL": "BNPL",
}
# Distribution of (non-timeout) failure codes per group
FAILURE_DIST = {
    "CARD": {"05": .22, "51": .25, "54": .04, "55": .08, "59": .05, "91": .07, "96": .06, "3DS_FAIL": .18, "GW_ERR": .05},
    "EMI": {"EMI_INELIGIBLE": .40, "05": .15, "51": .20, "3DS_FAIL": .15, "96": .05, "GW_ERR": .05},
    "UPI": {"U30": .20, "ZM": .20, "Z9": .28, "U16": .06, "U28": .10, "XY": .04, "GW_ERR": .05, "U69": .07},
    "UPI_COLLECT": {"U69": .50, "U30": .12, "ZM": .10, "Z9": .14, "U16": .04, "U28": .06, "GW_ERR": .04},
    "NETBANKING": {"NB_DROP": .45, "NB_AUTH": .25, "NB_TECH": .25, "GW_ERR": .05},
    "WALLET": {"W_BAL": .60, "W_LIMIT": .20, "GW_ERR": .20},
    "BNPL": {"BNPL_DECLINED": .70, "GW_ERR": .30},
}
TIMEOUT_CODE = {"CARD": "ACQ_TO", "EMI": "ACQ_TO", "UPI": "BANK_TO", "UPI_COLLECT": "BANK_TO",
                "NETBANKING": "BANK_TO", "WALLET": "GW_TO", "BNPL": "GW_TO"}
OUTAGE_CODE = {"CARD": "91", "EMI": "91", "UPI": "XY", "UPI_COLLECT": "XY",
               "NETBANKING": "NB_TECH", "WALLET": "GW_ERR", "BNPL": "GW_ERR"}

LATENCY_MEDIAN_MS = {
    "UPI_INTENT": 4000, "UPI_COLLECT": 40000, "UPI_QR": 3500, "CREDIT": 6000, "DEBIT": 6000,
    "PREPAID": 6000, "NETBANKING": 25000, "WALLET": 2000, "EMI": 7000, "BNPL": 5000,
}

CATEGORIES_POS = (["RETAIL", "FOOD", "HEALTHCARE"], [0.50, 0.35, 0.15])
CATEGORIES_ONLINE = (["ECOMMERCE", "TRAVEL", "EDUCATION", "UTILITIES", "GAMING", "RETAIL"],
                     [0.35, 0.12, 0.12, 0.13, 0.10, 0.18])
CATEGORY_MCC = {
    "RETAIL": ["5411", "5311", "5651", "5732"], "FOOD": ["5812", "5814"],
    "ECOMMERCE": ["5399", "5964"], "TRAVEL": ["4722", "4511"], "EDUCATION": ["8299", "8220"],
    "UTILITIES": ["4900", "4814"], "GAMING": ["7994"], "HEALTHCARE": ["8062", "5912"],
}
# median ticket in INR, log-normal sigma
CATEGORY_TICKET = {
    "RETAIL": (900, 0.8), "FOOD": (450, 0.6), "ECOMMERCE": (1500, 0.9), "TRAVEL": (6000, 0.8),
    "EDUCATION": (8000, 0.7), "UTILITIES": (1200, 0.6), "GAMING": (300, 0.9), "HEALTHCARE": (1100, 0.8),
}

NAME_PREFIXES = [
    "Urban", "Royal", "Green", "Sunrise", "Metro", "Blue", "Golden", "Silver", "Lotus", "Peacock",
    "Saffron", "Nova", "Prime", "Bright", "Spice", "Fresh", "Happy", "Star", "Trinity", "Everest",
    "Coastal", "Vista", "Swift", "Zen", "Kiran", "Anand", "Shree", "Neel", "Tara", "Indus",
]
CATEGORY_NOUNS = {
    "RETAIL": ["Mart", "Stores", "Supermarket", "Traders", "Emporium", "Bazaar", "Fashions", "Electronics"],
    "FOOD": ["Kitchen", "Cafe", "Bites", "Dhaba", "Sweets", "Bakery", "Restaurant"],
    "ECOMMERCE": ["Shop Online", "Cart", "Deals", "Webstore", "Direct"],
    "TRAVEL": ["Travels", "Holidays", "Tours", "Yatra"],
    "EDUCATION": ["Academy", "Classes", "Institute", "Learning"],
    "UTILITIES": ["Power", "Gas", "Broadband", "Telecom"],
    "GAMING": ["Games", "Play", "Arena"],
    "HEALTHCARE": ["Pharmacy", "Clinic", "Diagnostics", "Hospital"],
}

# city, state, region, tier, weight
CITIES = [
    ("Mumbai", "Maharashtra", "WEST", "TIER1", 10), ("Delhi", "Delhi", "NORTH", "TIER1", 10),
    ("Bengaluru", "Karnataka", "SOUTH", "TIER1", 9), ("Chennai", "Tamil Nadu", "SOUTH", "TIER1", 6),
    ("Hyderabad", "Telangana", "SOUTH", "TIER1", 6), ("Kolkata", "West Bengal", "EAST", "TIER1", 5),
    ("Pune", "Maharashtra", "WEST", "TIER1", 5), ("Ahmedabad", "Gujarat", "WEST", "TIER1", 4),
    ("Jaipur", "Rajasthan", "NORTH", "TIER2", 3), ("Lucknow", "Uttar Pradesh", "NORTH", "TIER2", 3),
    ("Indore", "Madhya Pradesh", "CENTRAL", "TIER2", 2), ("Bhopal", "Madhya Pradesh", "CENTRAL", "TIER2", 2),
    ("Kochi", "Kerala", "SOUTH", "TIER2", 2), ("Coimbatore", "Tamil Nadu", "SOUTH", "TIER2", 2),
    ("Nagpur", "Maharashtra", "WEST", "TIER2", 2), ("Surat", "Gujarat", "WEST", "TIER2", 2),
    ("Patna", "Bihar", "EAST", "TIER2", 2), ("Bhubaneswar", "Odisha", "EAST", "TIER2", 2),
    ("Guwahati", "Assam", "NORTHEAST", "TIER2", 2), ("Chandigarh", "Chandigarh", "NORTH", "TIER2", 2),
    ("Mysuru", "Karnataka", "SOUTH", "TIER3", 1), ("Nashik", "Maharashtra", "WEST", "TIER3", 1),
    ("Udaipur", "Rajasthan", "NORTH", "TIER3", 1), ("Siliguri", "West Bengal", "EAST", "TIER3", 1),
    ("Shillong", "Meghalaya", "NORTHEAST", "TIER3", 1), ("Raipur", "Chhattisgarh", "CENTRAL", "TIER3", 1),
]

ACCOUNT_MANAGERS = {
    "NORTH": ["Aarav Mehta", "Ishita Kapoor"],
    "WEST": ["Rohan Desai", "Sneha Kulkarni"],
    "SOUTH": ["Karthik Iyer", "Divya Nair"],
    "EAST": ["Arjun Sen"],
    "CENTRAL": ["Priya Bora"],
    "NORTHEAST": ["Priya Bora"],
}

HOLIDAYS = {
    "2026-08-15": "Independence Day",
    "2026-08-28": "Raksha Bandhan",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Gandhi Jayanti",
    "2026-10-20": "Dussehra",
}
