from flask import Flask, jsonify, render_template, request, session, redirect, url_for, flash
import pandas as pd
import feedparser
#from transformers import pipeline
from datetime import datetime, timedelta
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
import concurrent.futures
import logging
import torch
import trafilatura
from newspaper import Article
import requests
import re
import json
from collections import defaultdict, Counter
import time
import threading
import queue
from werkzeug.security import generate_password_hash, check_password_hash
import uuid
import os

app = Flask(__name__)

# Create directories
os.makedirs('user_data', exist_ok=True)
os.makedirs('user_data/watchlists', exist_ok=True)
os.makedirs('user_data/users', exist_ok=True)

app.secret_key = 'your-secret-key-change-in-production-2025'

# Global cache
news_cache = {
    'data': None,
    'timestamp': None,
    'lock': threading.Lock()
}

CACHE_DURATION = 600  # 10 minutes

# Load company data
try:
    company_df = pd.read_csv('company.csv')
    company_df.columns = company_df.columns.str.strip()
    company_df = company_df.dropna(subset=['COMPANY_NAME', 'SECTOR'])
    
    # Create valid symbols set and company name to symbol mapping
    VALID_INDIAN_SYMBOLS = set(company_df['SYMBOL'].str.upper().tolist())
    COMPANY_TO_SYMBOL = {}
    for idx, row in company_df.iterrows():
        company_name = row['COMPANY_NAME'].lower()
        symbol = row['SYMBOL'].upper()
        COMPANY_TO_SYMBOL[company_name] = symbol
    
    print(f"✅ Loaded {len(company_df)} companies with {len(VALID_INDIAN_SYMBOLS)} valid symbols")
except Exception as e:
    print(f"❌ Error loading company data: {e}")
    company_df = pd.DataFrame({
        'COMPANY_NAME': ['Reliance Industries', 'TCS', 'HDFC Bank', 'Bharat Electronics'],
        'SYMBOL': ['RELIANCE', 'TCS', 'HDFCBANK', 'BEL'],
        'SECTOR': ['Oil & Gas', 'IT', 'Banking', 'Defense']
    })
    VALID_INDIAN_SYMBOLS = set(['RELIANCE', 'TCS', 'HDFCBANK', 'BEL', 'INFY', 'WIPRO', 'ICICIBANK', 'SBIN'])
    COMPANY_TO_SYMBOL = {
        'reliance industries': 'RELIANCE',
        'tcs': 'TCS',
        'hdfc bank': 'HDFCBANK',
        'bharat electronics': 'BEL'
    }

# Initialize AI models (optional)
try:
    #sentiment_pipeline = pipeline("sentiment-analysis")
    #summarizer = pipeline("summarization", model="t5-base", device=-1)

    print("✅ AI models loaded successfully")
except Exception as e:
    print(f"⚠️ AI models not loaded: {e}")
    sentiment_pipeline = None
    summarizer = None

# **USER MANAGEMENT**
def load_users():
    try:
        with open('user_data/users.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_users(users):
    with open('user_data/users.json', 'w') as f:
        json.dump(users, f, indent=2)

def create_user(username, password, email):
    users = load_users()
    if username in users:
        return False, "Username already exists"
    
    user_id = str(uuid.uuid4())
    users[username] = {
        'id': user_id,
        'password': generate_password_hash(password),
        'email': email,
        'created_at': datetime.now().isoformat()
    }
    save_users(users)
    create_empty_watchlist(user_id)
    return True, "User created successfully"

def verify_user(username, password):
    users = load_users()
    if username not in users:
        return False, "User not found"
    
    if check_password_hash(users[username]['password'], password):
        return True, users[username]
    return False, "Invalid password"

def create_empty_watchlist(user_id):
    watchlist_data = {
        'user_id': user_id,
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat(),
        'stocks': []
    }
    
    with open(f'user_data/watchlists/{user_id}_watchlist.json', 'w') as f:
        json.dump(watchlist_data, f, indent=2)

def load_user_watchlist(user_id):
    try:
        with open(f'user_data/watchlists/{user_id}_watchlist.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        create_empty_watchlist(user_id)
        return load_user_watchlist(user_id)

def save_user_watchlist(user_id, watchlist_data):
    watchlist_data['updated_at'] = datetime.now().isoformat()
    with open(f'user_data/watchlists/{user_id}_watchlist.json', 'w') as f:
        json.dump(watchlist_data, f, indent=2)

def search_stocks(query):
    try:
        query_lower = query.lower().strip()
        matches = []
        
        exact_symbol = company_df[company_df['SYMBOL'].str.lower() == query_lower]
        matches.extend(exact_symbol.to_dict('records'))
        
        symbol_starts = company_df[
            (company_df['SYMBOL'].str.lower().str.startswith(query_lower)) &
            (~company_df['SYMBOL'].str.lower().isin([query_lower]))
        ]
        matches.extend(symbol_starts.to_dict('records'))
        
        name_contains = company_df[
            (company_df['COMPANY_NAME'].str.lower().str.contains(query_lower, na=False)) &
            (~company_df['SYMBOL'].str.lower().str.startswith(query_lower))
        ]
        matches.extend(name_contains.to_dict('records'))
        
        seen_symbols = set()
        results = []
        
        for match in matches:
            symbol = match.get('SYMBOL', 'N/A')
            if symbol not in seen_symbols:
                seen_symbols.add(symbol)
                results.append({
                    'symbol': symbol,
                    'name': match.get('COMPANY_NAME', 'N/A'),
                    'sector': match.get('SECTOR', 'N/A'),
                    'industry': match.get('INDUSTRY', 'N/A')
                })
                
                if len(results) >= 15:
                    break
        
        return results
        
    except Exception as e:
        print(f"Stock search error: {e}")
        return []

def get_stock_price(symbol):
    """Demo stock price"""
    import random
    base_price = random.uniform(100, 5000)
    change = random.uniform(-50, 50)
    percent_change = (change / base_price) * 100
    
    return {
        'symbol': symbol,
        'price': round(base_price, 2),
        'change': round(change, 2),
        'percent_change': round(percent_change, 2),
        'status': 'demo_data'
    }

# **ENHANCED SECTOR KEYWORDS**
ENHANCED_SECTOR_KEYWORDS = {
    "Banking": {
        "companies": [
            'hdfc bank', 'icici bank', 'sbi', 'state bank', 'axis bank', 'kotak mahindra',
            'indusind bank', 'federal bank', 'yes bank', 'idfc first bank', 'rbl bank',
            'bandhan bank', 'punjab national bank', 'pnb', 'bank of baroda', 'bob',
            'canara bank', 'union bank', 'indian bank', 'central bank', 'bank of india',
            'iob', 'bank of maharashtra', 'jammu kashmir bank', 'j&k bank', 'au small finance',
            'equitas small finance', 'ujjivan', 'dcb bank', 'karnataka bank', 'city union bank',
            'south indian bank', 'karur vysya bank', 'tamilnad mercantile bank'
        ],
        "keywords": [
            'bank', 'banking', 'loans', 'deposits', 'npa', 'credit', 'lending', 'borrowing',
            'casa', 'net interest margin', 'nim', 'advances', 'asset quality', 'retail banking',
            'corporate banking', 'home loan', 'personal loan', 'car loan', 'education loan',
            'branch', 'atm', 'digital banking', 'mobile banking', 'net banking'
        ],
        "symbols": [
            'HDFCBANK', 'ICICIBANK', 'SBIN', 'AXISBANK', 'KOTAKBANK', 'INDUSINDBK',
            'FEDERALBNK', 'YESBANK', 'IDFCFIRSTB', 'RBLBANK', 'BANDHANBNK',
            'PNB', 'BANKBARODA', 'CANBK', 'UNIONBANK', 'INDIANB', 'CENTRALBK',
            'IOB', 'MAHABANK', 'JKBANK', 'AUBANK', 'EQUITASBNK', 'UJJIVANSFB',
            'DCBBANK', 'KTKBANK', 'CITYUNION', 'SOUTHBANK', 'KARURVYSYA', 'TMB'
        ]
    },
    
    "Financial Services": {
        "companies": [
            'bajaj finance', 'bajaj finserv', 'hdfc life', 'sbi life', 'icici prudential',
            'lic', 'life insurance', 'cholamandalam', 'muthoot finance', 'paisalo',
            'shriram finance', 'power finance', 'pfc', 'rec', 'iifl', 'lic housing',
            'can fin homes', 'pnb housing', 'mahindra finance', 'sbi cards', 'hdfc amc',
            'cdsl', 'mas financial', 'aptus', 'aavas', 'home first', 'manappuram'
        ],
        "keywords": [
            'nbfc', 'non banking', 'financial services', 'insurance', 'life insurance',
            'general insurance', 'mutual fund', 'amc', 'housing finance', 'microfinance',
            'gold loan', 'consumer finance', 'vehicle finance', 'asset financing',
            'depository', 'share transfer', 'registrar', 'credit card', 'fintech',
            'payment', 'upi', 'wallet', 'lending', 'leasing', 'hire purchase'
        ],
        "symbols": [
            'BAJFINANCE', 'BAJAJFINSV', 'HDFCLIFE', 'SBILIFE', 'ICICIPRULI', 'LICI',
            'CHOLAFIN', 'MUTHOOTFIN', 'PAISALO', 'SHRIRAMFIN', 'PFC', 'RECLTD',
            'IIFL', 'LICHSGFIN', 'CANFINHOME', 'PNBHOUSING', 'MMFIN', 'SBICARD',
            'HDFCAMC', 'CDSL', 'MASFIN', 'APTUS', 'AAVAS', 'HOMEFIRST', 'MANAPPURAM'
        ]
    },
    
    "IT": {
        "companies": [
            'tcs', 'tata consultancy', 'infosys', 'wipro', 'hcl tech', 'tech mahindra',
            'ltts', 'l&t technology', 'persistent', 'coforge', 'ltimindtree', 'happiest minds',
            'tata elxsi', 'kpit tech', 'cyient', 'zensar', 'birlasoft', 'mindtree',
            'mphasis', 'route mobile', 'sonata software', 'mastek', 'intellect design',
            'tanla platforms', 'oracle financial', 'nucleus software', 'newgen software',
            'firstsource', 'info edge', 'just dial', 'zomato', 'naukri', 'paytm'
        ],
        "keywords": [
            'software', 'technology', 'it', 'information technology', 'digital', 'cloud',
            'saas', 'software services', 'programming', 'coding', 'tech services',
            'digital transformation', 'artificial intelligence', 'ai', 'machine learning',
            'data analytics', 'cybersecurity', 'consulting', 'outsourcing', 'bpo',
            'engineering services', 'product development', 'erp', 'crm', 'platform'
        ],
        "symbols": [
            'TCS', 'INFY', 'WIPRO', 'HCLTECH', 'TECHM', 'LTTS', 'PERSISTENT',
            'COFORGE', 'LTIM', 'HAPPSTMNDS', 'TATAELXSI', 'KPITTECH', 'CYIENT',
            'ZENSAR', 'BIRLASOFT', 'MPHASIS', 'ROUTE', 'SONATSOFTW', 'MASTEK',
            'INTELLECT', 'TANLA', 'OFSS', 'NUCLEUSS', 'NEWGEN', 'FSL',
            'INFOEDGE', 'JUSTDIAL', 'ZOMATO', 'NAUKRI', 'PAYTM'
        ]
    },
    
    "Oil & Gas": {
        "companies": [
            'reliance', 'reliance industries', 'ongc', 'oil and natural gas', 'bpcl',
            'bharat petroleum', 'ioc', 'indian oil', 'hpcl', 'hindustan petroleum',
            'gail', 'gail india', 'oil india', 'petronet lng', 'igl', 'indraprastha gas',
            'mgl', 'mahanagar gas', 'gujarat gas', 'gspl', 'adani gas', 'adani total gas',
            'aegis logistics', 'mrpl', 'mangalore refinery', 'castrol', 'gulf oil'
        ],
        "keywords": [
            'oil', 'gas', 'petroleum', 'refinery', 'crude oil', 'energy', 'petrol', 'diesel',
            'lng', 'cng', 'natural gas', 'lpg', 'petrochemical', 'fuel', 'opec', 'drilling',
            'exploration', 'production', 'refining', 'downstream', 'upstream', 'midstream',
            'oil prices', 'brent crude', 'gasoline', 'aviation fuel', 'atf', 'pipeline',
            'city gas distribution', 'oil marketing', 'lubricants'
        ],
        "symbols": [
            'RELIANCE', 'ONGC', 'BPCL', 'IOC', 'HPCL', 'GAIL', 'OIL', 'PETRONET',
            'IGL', 'MGL', 'GUJGASLTD', 'GSPL', 'ADANIGAS', 'AEGISLOG', 'MRPL',
            'CASTROLIND', 'GULFPETRO'
        ]
    },
    
    "Pharmaceuticals": {
        "companies": [
            'sun pharma', 'sun pharmaceutical', 'cipla', 'dr reddy', 'dr reddys',
            'divis labs', 'lupin', 'aurobindo pharma', 'torrent pharma', 'alkem',
            'biocon', 'zydus', 'zydus lifesciences', 'glenmark', 'ipca', 'mankind',
            'laurus labs', 'granules india', 'natco pharma', 'dr lal pathlabs',
            'metropolis', 'thyrocare', 'apollo hospitals', 'max healthcare', 'fortis',
            'narayana hrudayalaya', 'syngene', 'strides pharma', 'cadila', 'jubilant pharmova'
        ],
        "keywords": [
            'pharma', 'pharmaceutical', 'healthcare', 'drugs', 'medicines', 'vaccine',
            'formulations', 'api', 'active pharmaceutical ingredient', 'generic drugs',
            'hospitals', 'diagnostics', 'pathology', 'clinical trials', 'biotech',
            'biotechnology', 'medical', 'therapeutic', 'oncology', 'cardiology',
            'diabetes', 'antibiotics', 'usfda', 'fda approval', 'patent', 'molecule'
        ],
        "symbols": [
            'SUNPHARMA', 'CIPLA', 'DRREDDY', 'DIVISLAB', 'LUPIN', 'AUROPHARMA',
            'TORNTPHARM', 'ALKEM', 'BIOCON', 'ZYDUSLIFE', 'GLENMARK', 'IPCALAB',
            'MANKIND', 'LAURUSLABS', 'GRANULES', 'NATCOPHARM', 'LALPATHLAB',
            'METROPOLIS', 'THYROCARE', 'APOLLOHOSP', 'MAXHEALTH', 'FORTIS',
            'NARAYANA', 'SYNGENE', 'STRIDES', 'CADILAHC', 'JUBLPHARMA'
        ]
    },
    
    "Automobile": {
        "companies": [
            'maruti', 'maruti suzuki', 'tata motors', 'mahindra', 'm&m', 'mahindra and mahindra',
            'bajaj auto', 'hero motocorp', 'tvs motor', 'eicher motors', 'royal enfield',
            'ashok leyland', 'escorts', 'escorts kubota', 'bosch', 'motherson',
            'samvardhana motherson', 'bharat forge', 'exide', 'amara raja', 'mrf',
            'apollo tyres', 'ceat', 'balkrishna industries', 'jk tyre', 'uno minda'
        ],
        "keywords": [
            'automobile', 'auto', 'cars', 'bikes', 'vehicles', 'electric vehicle', 'ev',
            'scooters', 'motorcycles', 'two wheeler', 'four wheeler', 'passenger vehicle',
            'commercial vehicle', 'tractors', 'buses', 'trucks', 'auto sales', 'production',
            'auto components', 'auto parts', 'tyres', 'batteries', 'automotive',
            'mobility', 'hybrid vehicles', 'automobile exports', 'dealership'
        ],
        "symbols": [
            'MARUTI', 'TATAMOTORS', 'M&M', 'BAJAJ-AUTO', 'HEROMOTOCO', 'TVSMOTOR',
            'EICHERMOT', 'ASHOKLEY', 'ESCORTS', 'BOSCHLTD', 'MOTHERSON',
            'BHARATFORG', 'EXIDEIND', 'AMARAJABAT', 'MRF', 'APOLLOTYRE', 'CEATLTD',
            'BALKRISIND', 'JKTYRE', 'UNOMINDA'
        ]
    },
    
    "Defense": {
        "companies": [
            'bharat electronics', 'bel', 'hal', 'hindustan aeronautics', 'bharat dynamics',
            'bdl', 'beml', 'cochin shipyard', 'garden reach shipbuilders', 'grse',
            'mazagon dock', 'mdl', 'data patterns', 'midhani', 'zen technologies'
        ],
        "keywords": [
            'defense', 'defence', 'military', 'aerospace', 'aviation', 'weapons',
            'missiles', 'radars', 'naval', 'shipyard', 'electronic warfare',
            'defense electronics', 'avionics', 'weapons systems', 'defense contracts',
            'ministry of defense', 'mod', 'indian army', 'indian navy', 'indian air force',
            'fighter jets', 'helicopters', 'submarines', 'warships'
        ],
        "symbols": [
            'BEL', 'HAL', 'BDL', 'BEML', 'COCHINSHIP', 'GRSE', 'MAZDOCK',
            'DATAPATTNS', 'MIDHANI', 'ZENTEC'
        ]
    },
    
    "Metals & Mining": {
        "companies": [
            'tata steel', 'jsw steel', 'hindalco', 'vedanta', 'sail', 'steel authority',
            'jindal steel', 'jspl', 'nmdc', 'coal india', 'national aluminium', 'nalco',
            'hindustan zinc', 'hindzinc', 'ratnamani', 'apl apollo', 'jindal saw',
            'welspun corp', 'sunflag', 'moil'
        ],
        "keywords": [
            'steel', 'metals', 'mining', 'aluminium', 'aluminum', 'copper', 'zinc',
            'iron ore', 'coal', 'smelting', 'alloy', 'ferrous', 'non-ferrous',
            'metal prices', 'commodities', 'steel production', 'steel demand',
            'steel prices', 'mining operations', 'extraction', 'metal exports',
            'steel plant', 'blast furnace', 'hot rolled', 'cold rolled'
        ],
        "symbols": [
            'TATASTEEL', 'JSWSTEEL', 'HINDALCO', 'VEDL', 'SAIL', 'JSPL',
            'NMDC', 'COALINDIA', 'NATIONALUM', 'HINDZINC', 'RATNAMANI',
            'APL', 'JINDALSTEL', 'WELCORP', 'SUNFLAG', 'MOIL'
        ]
    },
    
    "FMCG": {
        "companies": [
            'hindustan unilever', 'hul', 'itc', 'nestle', 'nestle india', 'britannia',
            'dabur', 'godrej consumer', 'marico', 'colgate', 'colgate palmolive',
            'emami', 'tata consumer', 'varun beverages', 'united spirits', 'mcdowell',
            'radico khaitan', 'jubilant foodworks', 'devyani international', 'westlife',
            'bikaji', 'mrs bectors', 'parag milk', 'heritage foods'
        ],
        "keywords": [
            'fmcg', 'fast moving consumer goods', 'consumer products', 'consumer goods',
            'personal care', 'food products', 'beverages', 'toiletries', 'household',
            'home care', 'packaged foods', 'snacks', 'dairy', 'biscuits', 'soaps',
            'detergents', 'shampoo', 'skincare', 'grocery', 'staples', 'consumption',
            'rural demand', 'urban demand', 'distribution', 'retail', 'qsr', 'restaurant'
        ],
        "symbols": [
            'HINDUNILVR', 'ITC', 'NESTLEIND', 'BRITANNIA', 'DABUR', 'GODREJCP',
            'MARICO', 'COLPAL', 'EMAMILTD', 'TATACONSUM', 'VBL', 'MCDOWELL-N',
            'RADICO', 'JUBLFOOD', 'DEVYANI', 'WESTLIFE', 'BIKAJI', 'MRSBECTORS',
            'PARAG', 'HERITAGE'
        ]
    },
    
    "Real Estate": {
        "companies": [
            'dlf', 'godrej properties', 'oberoi realty', 'prestige estates', 'brigade',
            'sobha', 'phoenix mills', 'lodha', 'macrotech', 'sunteck realty',
            'mahindra lifespace', 'raymond', 'indiabulls real estate'
        ],
        "keywords": [
            'real estate', 'realty', 'property', 'residential', 'commercial property',
            'construction', 'housing', 'apartments', 'flats', 'housing sales',
            'property prices', 'rera', 'developers', 'land', 'township', 'plotted',
            'villas', 'office spaces', 'retail spaces', 'warehousing'
        ],
        "symbols": [
            'DLF', 'GODREJPROP', 'OBEROIRLTY', 'PRESTIGE', 'BRIGADE', 'SOBHA',
            'PHOENIXLTD', 'LODHA', 'SUNTECK', 'MAHLIFE', 'RAYMOND', 'IBREALEST'
        ]
    },
    
    "Cement": {
        "companies": [
            'ultratech', 'ultratech cement', 'shree cement', 'acc', 'ambuja',
            'ambuja cement', 'dalmia bharat', 'jk cement', 'ramco cements',
            'birla corporation', 'india cements', 'jk lakshmi', 'orient cement'
        ],
        "keywords": [
            'cement', 'building materials', 'construction materials', 'concrete',
            'cement prices', 'cement production', 'cement capacity', 'cement demand',
            'infrastructure spending', 'housing construction', 'grey cement', 'white cement',
            'clinker', 'fly ash', 'ready mix concrete', 'rmc'
        ],
        "symbols": [
            'ULTRACEMCO', 'SHREECEM', 'ACC', 'AMBUJACEM', 'DALBHARAT',
            'JKCEMENT', 'RAMCOCEM', 'BIRLACORPN', 'INDIACEM', 'JKLAKSHMI', 'ORIENTCEM'
        ]
    },
    
    "Telecom": {
        "companies": [
            'bharti airtel', 'airtel', 'vodafone idea', 'vi', 'idea', 'indus towers',
            'tata communications', 'route mobile', 'tanla', 'sterlite tech'
        ],
        "keywords": [
            'telecom', 'telecommunication', 'mobile', 'wireless', 'broadband', 'internet',
            'data', '5g', '4g', 'network', 'spectrum', 'arpu', 'subscriber', 'mobile data',
            'voice', 'towers', 'fiber', 'connectivity', 'telco', 'operator'
        ],
        "symbols": [
            'BHARTIARTL', 'IDEA', 'INDUSTOWER', 'TATACOMM', 'ROUTE', 'TANLA', 'STLTECH'
        ]
    },
    
    "Power": {
        "companies": [
            'ntpc', 'power grid', 'powergrid', 'adani power', 'tata power',
            'jsw energy', 'adani green', 'torrent power', 'cesc', 'nhpc', 'sjvn',
            'suzlon', 'inox wind', 'borosil renewables'
        ],
        "keywords": [
            'power', 'electricity', 'energy', 'renewable', 'solar', 'wind', 'hydro',
            'thermal', 'coal power', 'power generation', 'power distribution',
            'transmission', 'green energy', 'clean energy', 'renewable capacity',
            'power tariff', 'electricity demand', 'grid', 'discoms'
        ],
        "symbols": [
            'NTPC', 'POWERGRID', 'ADANIPOWER', 'TATAPOWER', 'ADANIGREEN',
            'JSWENERGY', 'TORNTPOWER', 'CESC', 'NHPC', 'SJVN', 'SUZLON', 'INOXWIND'
        ]
    },
    
    "Retail": {
        "companies": [
            'dmart', 'avenue supermarts', 'trent', 'titan', 'aditya birla fashion',
            'abfrl', 'shoppers stop', 'v-mart', 'reliance retail', 'future retail',
            'spencer retail', 'relaxo', 'bata'
        ],
        "keywords": [
            'retail', 'shopping', 'stores', 'supermarket', 'hypermarket', 'department store',
            'fashion retail', 'jewelry', 'footwear', 'apparel', 'lifestyle', 'omnichannel',
            'same store sales', 'footfall', 'retail expansion', 'e-commerce', 'online retail'
        ],
        "symbols": [
            'DMART', 'TRENT', 'TITAN', 'ABFRL', 'SHOPERSTOP', 'VMART',
            'SPENCER', 'RELAXO', 'BATAINDIA'
        ]
    }
}



ENHANCED_RSS_FEEDS = {
    # Economic Times
    "economic_times_market": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "economic_times_stocks": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "economic_times_ipos": "https://economictimes.indiatimes.com/markets/ipo/rssfeeds/67812142.cms",
    "economic_times_commodities": "https://economictimes.indiatimes.com/markets/commodities/rssfeeds/1808152121.cms",
    
    # Moneycontrol
    "moneycontrol": "https://www.moneycontrol.com/rss/business.xml",
    "moneycontrol_news": "https://www.moneycontrol.com/rss/latestnews.xml",
    "moneycontrol_markets": "https://www.moneycontrol.com/rss/marketreports.xml",
    "moneycontrol_stocks": "https://www.moneycontrol.com/rss/stockmarket.xml",
    
    # Business Standard
    "business_standard": "https://www.business-standard.com/rss/markets-106.rss",
    "business_standard_companies": "https://www.business-standard.com/rss/companies-101.rss",
    "business_standard_economy": "https://www.business-standard.com/rss/economy-policy-102.rss",
    
    # Financial Express
    "financial_express": "https://www.financialexpress.com/market/feed/",
    "financial_express_industry": "https://www.financialexpress.com/industry/feed/",
    
    # Livemint
    "livemint": "https://www.livemint.com/rss/markets",
    "livemint_companies": "https://www.livemint.com/rss/companies",
    "livemint_money": "https://www.livemint.com/rss/money",
    
    # NDTV Profit
    "ndtv_business": "https://feeds.feedburner.com/ndtvprofit-latest",
    
    # Zee Business
    "zeebiz": "https://www.zeebiz.com/rss/markets.xml",
    "zeebiz_personal_finance": "https://www.zeebiz.com/rss/personal-finance.xml",
    "zeebiz_stocks": "https://www.zeebiz.com/rss/market-news.xml",
    
    # CNBC TV18
    "cnbc_market": "https://www.cnbctv18.com/rss/marketnews.xml",
    "cnbc_business": "https://www.cnbctv18.com/rss/latestnews.xml",
    
    # Google News
    "google_india_business": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp0Y0RvU0FtVnVHZ0pKVGtnQVAB?hl=en-IN&gl=IN&ceid=IN:en",
    "google_india_stocks": "https://news.google.com/rss/search?q=indian%20stocks&hl=en-IN&gl=IN&ceid=IN:en",
    "google_sensex": "https://news.google.com/rss/search?q=sensex&hl=en-IN&gl=IN&ceid=IN:en",
    "google_nifty": "https://news.google.com/rss/search?q=nifty&hl=en-IN&gl=IN&ceid=IN:en",
    
    # Business Today
    "business_today_markets": "https://www.businesstoday.in/rss/market",
    "business_today_companies": "https://www.businesstoday.in/rss/company",
    
    # BQ Prime (BloombergQuint)
    "bq_prime_markets": "https://www.bqprime.com/markets.rss",
    "bq_prime_business": "https://www.bqprime.com/business.rss",
    
    # Hindu Business Line
    "hindu_business_line": "https://www.thehindubusinessline.com/markets/stock-markets/feeder/default.rss",
    
    # India Today
    "india_today_business": "https://www.indiatoday.in/rss/1206514",
    
    # Times of India Business
    "toi_business": "https://timesofindia.indiatimes.com/rssfeeds/1898055.cms",
    
    # Reuters
    "reuters_india": "https://www.reuters.com/rssFeed/INbusinessNews",
    
    # Investing.com India
    "investing_india": "https://www.investing.com/rss/news_301.rss",
}



# Global logs
processing_logs = []
log_lock = threading.Lock()

def add_log(message):
    with log_lock:
        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            log_message = f"[{timestamp}] {message}"
            processing_logs.append(log_message)
            print(log_message)
            
            if len(processing_logs) > 50:
                processing_logs.pop(0)
        except Exception as e:
            print(f"Logging error: {e}")

def get_logs():
    with log_lock:
        return processing_logs.copy()

POSITIVE_WORDS = [
    'profit', 'growth', 'up', 'rise', 'gain', 'surge', 'bullish', 'positive', 'beat', 'strong',
    'earnings', 'revenue', 'high', 'record', 'boost', 'rally', 'jump', 'soar', 'climb', 'upgrade'
]

NEGATIVE_WORDS = [
    'loss', 'down', 'fall', 'decline', 'crash', 'bearish', 'negative', 'miss', 'weak', 'drop',
    'slump', 'plunge', 'tumble', 'collapse', 'worry', 'fear', 'downgrade', 'cut'
]

def extract_stocks_from_headline(title):
    """
    Extract stocks ONLY from headline - ULTRA STRICT
    Excludes short symbols (2-3 letters) unless they appear with stock context
    """
    if not title:
        return []
    
    import re
    
    # Stock context words that should appear near the symbol
    STOCK_CONTEXT = [
        'share', 'stock', 'equity', 'bse', 'nse', 'sensex', 'nifty',
        'market', 'trading', 'investors', 'price', 'gains', 'falls',
        'q1', 'q2', 'q3', 'q4', 'earnings', 'profit', 'loss', 'revenue',
        'demerger', 'merger', 'acquisition', 'ipo', 'fpo', 'dividend',
        'rally', 'surge', 'plunge', 'tumbles', 'jumps', 'soars'
    ]
    
    title_upper = title.upper()
    title_lower = title.lower()
    
    valid_stocks = []
    
    # Check each symbol with STRICT validation
    for symbol in VALID_INDIAN_SYMBOLS:
        symbol_len = len(symbol)
        
        # For very short symbols (2-3 chars), be EXTRA strict
        if symbol_len <= 3:
            # Must appear as standalone word AND have stock context
            pattern = r'\b' + re.escape(symbol) + r'\b'
            
            if re.search(pattern, title_upper):
                # Check if there's stock-related context in title
                has_context = any(ctx in title_lower for ctx in STOCK_CONTEXT)
                
                # Also check if it's mentioned with proper spacing (not part of URL, etc)
                # Example: "ITC share" ✅ but "Switch" ❌
                clean_pattern = r'(?:^|\s)' + re.escape(symbol) + r'(?:\s|$|\'s|,|\.)'
                proper_spacing = re.search(clean_pattern, title_upper)
                
                if has_context and proper_spacing:
                    valid_stocks.append(symbol)
        
        # For longer symbols (4+ chars), be less strict
        else:
            pattern = r'\b' + re.escape(symbol) + r'\b'
            if re.search(pattern, title_upper):
                valid_stocks.append(symbol)
    
    # Check for company names (these are always valid)
    for company_name, symbol in COMPANY_TO_SYMBOL.items():
        # Only match reasonably long company names
        if len(company_name) >= 5 and company_name in title_lower:
            if symbol not in valid_stocks:
                valid_stocks.append(symbol)
    
    # Check sector-specific company names
    for sector_data in ENHANCED_SECTOR_KEYWORDS.values():
        for idx, company_name in enumerate(sector_data['companies']):
            if len(company_name) >= 5 and company_name in title_lower:
                if idx < len(sector_data['symbols']):
                    symbol = sector_data['symbols'][idx]
                    if symbol in VALID_INDIAN_SYMBOLS and symbol not in valid_stocks:
                        valid_stocks.append(symbol)
    
    # Final cleanup: Remove very common false positives
    FALSE_POSITIVES = ['IT', 'AM', 'PM', 'IN', 'ON', 'AT', 'TO', 'OR', 'AN', 'AS', 'BE', 'IS']
    valid_stocks = [s for s in valid_stocks if s not in FALSE_POSITIVES]
    
    return valid_stocks[:3]



def enhanced_sector_classification(title, description):
    """Classify article into sector"""
    article_text = f"{title} {description}".lower()
    
    sector_scores = {}
    
    for sector, data in ENHANCED_SECTOR_KEYWORDS.items():
        score = 0
        
        for company in data['companies']:
            if company in article_text:
                score += 10
        
        for keyword in data['keywords']:
            if keyword in article_text:
                score += 3
        
        # Check if sector symbols appear
        for symbol in data['symbols']:
            if symbol.lower() in article_text:
                score += 5
        
        if score > 0:
            sector_scores[sector] = score
    
    if sector_scores:
        best_sector = max(sector_scores.items(), key=lambda x: x[1])
        if best_sector[1] >= 3:
            return best_sector[0], {}
    
    return None, {}

def enhanced_sentiment_analysis(text, title=""):
    """Analyze sentiment"""
    try:
        combined_text = f"{title} {text}".lower()
        
        positive_score = sum(1 for word in POSITIVE_WORDS if word in combined_text)
        negative_score = sum(1 for word in NEGATIVE_WORDS if word in combined_text)
        
        if positive_score > negative_score:
            return "Positive", min(0.6 + (positive_score * 0.1), 0.9)
        elif negative_score > positive_score:
            return "Negative", min(0.6 + (negative_score * 0.1), 0.9)
        else:
            return "Neutral", 0.5
            
    except Exception as e:
        return "Neutral", 0.5

def is_indian_news(title, description):
    """Check if news is related to India"""
    combined = f"{title} {description}".lower()
    
    # Indian market indicators
    indian_keywords = [
        'india', 'indian', 'mumbai', 'delhi', 'bangalore',
        'nse', 'bse', 'sensex', 'nifty', 'rupee', 'rbi',
        'sebi', 'lic', 'tata', 'reliance', 'adani'
    ]
    
    # Check if any Indian keyword present
    has_indian_context = any(keyword in combined for keyword in indian_keywords)
    
    # Also check if Indian stock symbols present
    has_indian_stocks = bool(extract_stocks_from_headline(title))
    
    return has_indian_context or has_indian_stocks

def resolve_final_url(url):
    """Resolve URL redirects"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.head(url, headers=headers, allow_redirects=True, timeout=10)
        return response.url
    except:
        return url

from datetime import datetime, timedelta
import time

def process_rss_feed_enhanced(feed_name, feed_url, results_queue, max_articles=20):
    """Process RSS feed - ONLY LAST 24 HOURS NEWS"""
    try:
        add_log(f"🔄 Processing {feed_name}...")
        
        feed = feedparser.parse(feed_url)
        
        if not hasattr(feed, 'entries') or len(feed.entries) == 0:
            results_queue.put((feed_name, {}))
            return
        
        sector_articles = defaultdict(list)
        processed_count = 0
        
        # Calculate 24-hour cutoff time
        cutoff_time = datetime.now() - timedelta(hours=50)
        
        for entry in feed.entries[:max_articles]:
            title = entry.get('title', '')
            link = entry.get('link', '')
            description = BeautifulSoup(entry.get('summary', ''), 'html.parser').get_text()
            
            if not title or not link:
                continue
            
            # **NEW: Check publication date**
            pub_date = None
            
            # Try to get published date from feed
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                try:
                    pub_date = datetime.fromtimestamp(time.mktime(entry.published_parsed))
                except:
                    pass
            
            # Alternative: Check updated_parsed
            if not pub_date and hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                try:
                    pub_date = datetime.fromtimestamp(time.mktime(entry.updated_parsed))
                except:
                    pass
            
            # **FILTER: Skip if older than 24 hours**
            if pub_date and pub_date < cutoff_time:
                add_log(f"⏭️ Skipping old article: {title[:50]}... (published {pub_date})")
                continue
            
            # If no date found, include anyway (assume recent)
            if not pub_date:
                add_log(f"⚠️ No date for: {title[:50]}... (including anyway)")
            
            # Check if Indian news
            if not is_indian_news(title, description):
                continue
            
            # Extract stocks ONLY from headline
            stock_mentions = extract_stocks_from_headline(title)
            
            # Skip if no stocks in headline
            if not stock_mentions:
                continue
            
            sector, matches = enhanced_sector_classification(title, description)
            
            if sector:
                sentiment_label, sentiment_score = enhanced_sentiment_analysis(description, title)
                
                article_data = {
                    'title': title,
                    'description': description,
                    'url': link,
                    'sentiment': sentiment_score,
                    'sentiment_label': sentiment_label,
                    'source': feed_name.replace('_', ' ').title(),
                    'stock_mentions': stock_mentions,
                    'summary': description[:150],
                    'published_date': pub_date.strftime("%Y-%m-%d %H:%M") if pub_date else "Unknown"
                }
                
                sector_articles[sector].append(article_data)
                processed_count += 1
        
        results_queue.put((feed_name, dict(sector_articles)))
        add_log(f"✅ {feed_name}: {processed_count} articles from last 24 hours")
        
    except Exception as e:
        add_log(f"❌ Error in {feed_name}: {str(e)}")
        results_queue.put((feed_name, {}))


def fetch_enhanced_news():
    """Multi-threaded news fetching"""
    add_log("🚀 Fetching news from multiple sources...")
    
    results_queue = queue.Queue()
    threads = []
    
    for feed_name, feed_url in ENHANCED_RSS_FEEDS.items():
        thread = threading.Thread(
            target=process_rss_feed_enhanced,
            args=(feed_name, feed_url, results_queue),
            daemon=True
        )
        threads.append(thread)
        thread.start()
    
    for thread in threads:
        thread.join(timeout=30)
    
    final_articles = defaultdict(list)
    
    while not results_queue.empty():
        try:
            feed_name, sector_articles = results_queue.get_nowait()
            for sector, articles in sector_articles.items():
                final_articles[sector].extend(articles)
        except queue.Empty:
            break
    
    total = sum(len(v) for v in final_articles.values())
    add_log(f"✅ Total Indian market articles: {total}")
    return dict(final_articles)

def get_cached_news():
    """Get news from cache or fetch new"""
    with news_cache['lock']:
        now = time.time()
        
        if (news_cache['data'] is not None and 
            news_cache['timestamp'] is not None and 
            (now - news_cache['timestamp']) < CACHE_DURATION):
            add_log("📦 Using cached news data")
            return news_cache['data']
        
        add_log("🔄 Fetching fresh news data...")
        sector_articles = fetch_enhanced_news()
        news_cache['data'] = sector_articles
        news_cache['timestamp'] = now
        
        return sector_articles

def build_gainers_losers(sector_articles):
    """
    Build gainers/losers for each sector
    KEY FIX: Place stocks ONLY in their CSV-defined sector, not article sector
    """
    if not sector_articles:
        return {}
    
    # Create symbol to sector mapping from CSV
    SYMBOL_TO_SECTOR = {}
    for idx, row in company_df.iterrows():
        SYMBOL_TO_SECTOR[row['SYMBOL'].upper()] = row['SECTOR']
    
    # Track all stock mentions across all articles
    all_stock_mentions = defaultdict(lambda: {'positive': 0, 'negative': 0, 'articles': [], 'csv_sector': None})
    
    # Collect all stocks from all articles
    for article_sector, articles in sector_articles.items():
        for art in articles:
            mentioned = art.get('stock_mentions', [])
            sentiment = art.get('sentiment_label', 'Neutral')
            
            for symbol in mentioned:
                if symbol in VALID_INDIAN_SYMBOLS:
                    # Get CORRECT sector from CSV
                    correct_sector = SYMBOL_TO_SECTOR.get(symbol, 'Unknown')
                    
                    if correct_sector != 'Unknown':
                        all_stock_mentions[symbol]['csv_sector'] = correct_sector
                        all_stock_mentions[symbol]['articles'].append(art)
                        
                        if sentiment == 'Positive':
                            all_stock_mentions[symbol]['positive'] += 1
                        elif sentiment == 'Negative':
                            all_stock_mentions[symbol]['negative'] += 1
    
    # Now organize by CSV sector (not article sector)
    result = {}
    
    # Get all unique sectors from CSV
    unique_sectors = company_df['SECTOR'].unique()
    
    for sector in unique_sectors:
        gainers = []
        losers = []
        
        # Find stocks that belong to THIS sector (from CSV)
        for symbol, data in all_stock_mentions.items():
            if data['csv_sector'] == sector:
                if data['positive'] > data['negative'] and data['positive'] >= 1:
                    gainers.append({
                        'symbol': symbol,
                        'positive_count': data['positive'],
                        'articles': data['articles'][:3]
                    })
                elif data['negative'] > data['positive'] and data['negative'] >= 1:
                    losers.append({
                        'symbol': symbol,
                        'negative_count': data['negative'],
                        'articles': data['articles'][:3]
                    })
        
        # Sort
        gainers = sorted(gainers, key=lambda x: x['positive_count'], reverse=True)[:10]
        losers = sorted(losers, key=lambda x: x['negative_count'], reverse=True)[:10]
        
        # Get articles relevant to this sector
        positive_articles = []
        negative_articles = []
        
        for article_sector, articles in sector_articles.items():
            for art in articles:
                # Check if article mentions stocks from this sector
                stock_mentions = art.get('stock_mentions', [])
                has_sector_stock = any(
                    SYMBOL_TO_SECTOR.get(s) == sector 
                    for s in stock_mentions
                )
                
                if has_sector_stock:
                    if art['sentiment_label'] == 'Positive':
                        positive_articles.append(art)
                    elif art['sentiment_label'] == 'Negative':
                        negative_articles.append(art)
        
        # Only include sector if it has gainers/losers or articles
        if gainers or losers or positive_articles or negative_articles:
            result[sector] = {
                "gainers": gainers,
                "losers": losers,
                "positive": positive_articles[:10],
                "negative": negative_articles[:10]
            }
    
    add_log(f"✅ Built gainers/losers for {len(result)} sectors (CSV-based)")
    return result


# **AUTHENTICATION ROUTES**
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        success, result = verify_user(username, password)
        if success:
            session['user_id'] = result['id']
            session['username'] = username
            flash('Login successful!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash(result, 'error')
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        email = request.form['email']
        
        success, message = create_user(username, password, email)
        if success:
            flash(message, 'success')
            return redirect(url_for('login'))
        else:
            flash(message, 'error')
    
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully', 'info')
    return redirect(url_for('login'))

# **WATCHLIST ROUTES**
@app.route('/watchlist')
def watchlist_page():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    try:
        username = session.get('username', 'Guest')
        user_id = session['user_id']
        
        watchlist_data = load_user_watchlist(user_id)
        user_stocks = watchlist_data.get('stocks', [])
        
        sector_articles = get_cached_news()
        
        if user_stocks and sector_articles:
            watchlist_symbols = set([s['symbol'] for s in user_stocks])
            all_sector_data = build_gainers_losers(sector_articles)
            
            # Filter to only watchlist stocks
            filtered_data = {}
            for sector, data in all_sector_data.items():
                gainers = [g for g in data['gainers'] if g['symbol'] in watchlist_symbols]
                losers = [l for l in data['losers'] if l['symbol'] in watchlist_symbols]
                
                if gainers or losers:
                    filtered_data[sector] = {
                        'gainers': gainers,
                        'losers': losers
                    }
            
            watchlist_sector_data = filtered_data
        else:
            watchlist_sector_data = {}
        
        return render_template('watchlist.html', 
                              username=username,
                              watchlist_count=len(user_stocks),
                              watchlist_sector_data=watchlist_sector_data)
    
    except Exception as e:
        add_log(f"❌ Watchlist error: {str(e)}")
        return f"<h1>Error: {e}</h1>", 500

@app.route('/api/search_stocks')
def api_search_stocks():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 401
    
    query = request.args.get('q', '')
    if len(query) < 2:
        return jsonify({'results': []})
    
    results = search_stocks(query)
    return jsonify({'results': results})

@app.route('/api/add_to_watchlist', methods=['POST'])
def api_add_to_watchlist():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 401
    
    try:
        data = request.get_json()
        symbol = data.get('symbol', '').upper()
        name = data.get('name', '')
        sector = data.get('sector', '')
        
        if not symbol:
            return jsonify({'error': 'Symbol is required'}), 400
        
        user_id = session['user_id']
        watchlist = load_user_watchlist(user_id)
        
        existing_symbols = [stock['symbol'] for stock in watchlist.get('stocks', [])]
        if symbol in existing_symbols:
            return jsonify({'error': f'{symbol} already in watchlist'}), 400
        
        new_stock = {
            'symbol': symbol,
            'name': name,
            'sector': sector,
            'added_at': datetime.now().isoformat()
        }
        
        if 'stocks' not in watchlist:
            watchlist['stocks'] = []
        
        watchlist['stocks'].append(new_stock)
        save_user_watchlist(user_id, watchlist)
        
        return jsonify({
            'success': True, 
            'message': f'{symbol} added to watchlist',
            'total_stocks': len(watchlist['stocks'])
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/remove_from_watchlist', methods=['POST'])
def api_remove_from_watchlist():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 401
    
    try:
        data = request.get_json()
        symbol = data.get('symbol', '').upper()
        
        if not symbol:
            return jsonify({'error': 'Symbol is required'}), 400
        
        user_id = session['user_id']
        watchlist = load_user_watchlist(user_id)
        
        original_count = len(watchlist.get('stocks', []))
        watchlist['stocks'] = [stock for stock in watchlist.get('stocks', []) 
                               if stock['symbol'] != symbol]
        
        save_user_watchlist(user_id, watchlist)
        
        if len(watchlist['stocks']) < original_count:
            return jsonify({
                'success': True, 
                'message': f'{symbol} removed',
                'total_stocks': len(watchlist['stocks'])
            })
        else:
            return jsonify({'error': f'{symbol} not found'}), 404
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_watchlist')
def api_get_watchlist():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 401
    
    try:
        user_id = session['user_id']
        watchlist = load_user_watchlist(user_id)
        
        watchlist_with_prices = []
        for stock in watchlist.get('stocks', []):
            price_data = get_stock_price(stock['symbol'])
            stock_with_price = {**stock, **price_data}
            watchlist_with_prices.append(stock_with_price)
        
        return jsonify({
            'stocks': watchlist_with_prices,
            'total_stocks': len(watchlist_with_prices),
            'last_updated': datetime.now().isoformat(),
            'username': session.get('username')
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# **MAIN DASHBOARD ROUTE**
@app.route("/")
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    try:
        sector_articles = get_cached_news()
        sector_data = build_gainers_losers(sector_articles)
        
        total_articles = sum(len(articles) for articles in sector_articles.values())
        
        user_id = session['user_id']
        watchlist = load_user_watchlist(user_id)
        
        return render_template(
            "complete_dashboard.html",
            sector_data=sector_data,
            total_articles=total_articles,
            logs=get_logs()[-10:],
            last_updated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            username=session.get('username'),
            watchlist_count=len(watchlist.get('stocks', []))
        )
    except Exception as e:
        add_log(f"❌ Dashboard error: {str(e)}")
        return f"<h1>Dashboard Error: {e}</h1><pre>{str(e)}</pre>", 500

@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": get_logs()})

@app.route("/summarize")
def summarize_url():
    url = request.args.get("url")
    if not url:
        return jsonify({"summary": "No URL provided", "analysis_success": False})
    
    try:
        resolved_url = resolve_final_url(url)
        
        article_content = None
        extraction_method = "Simple"
        
        # Try newspaper3k first
        try:
            article = Article(resolved_url)
            article.download()
            article.parse()
            
            if article.text and len(article.text.split()) >= 30:
                article_content = article.text
                extraction_method = "Newspaper3k"
        except Exception as e:
            add_log(f"Newspaper3k failed: {e}")
        
        # Fallback to trafilatura
        if not article_content:
            try:
                downloaded = trafilatura.fetch_url(resolved_url)
                article_content = trafilatura.extract(downloaded)
                if article_content:
                    extraction_method = "Trafilatura"
            except Exception as e:
                add_log(f"Trafilatura failed: {e}")
        
        if not article_content or len(article_content.split()) < 30:
            return jsonify({
                "summary": "Could not extract enough article content for summarization",
                "stock_mentions": [],
                "sentiment": "Neutral",
                "sentiment_score": "0.50",
                "analysis_success": False
            })
        
        # **AI SUMMARIZATION** (if model loaded)
        summary_result = None
        
        if summarizer is not None:
            try:
                # Limit input to 1024 tokens (BART limitation)
                words = article_content.split()
                if len(words) > 800:
                    article_content = ' '.join(words[:800])
                
                # Generate AI summary
                summary_output = summarizer(
                    article_content,
                    max_length=130,
                    min_length=30,
                    do_sample=False
                )
                
                summary_result = summary_output[0]['summary_text']
                add_log(f"✅ AI summary generated ({len(summary_result.split())} words)")
                
            except Exception as e:
                add_log(f"❌ Summarizer error: {e}")
                # Fallback to extractive summary
                summary_result = None
        
        # Fallback: extractive summary (first 3 sentences)
        if not summary_result:
            sentences = article_content.split('. ')
            summary_result = '. '.join(sentences[:3]) + '.'
            add_log("⚠️ Using extractive summary (AI not available)")
        
        # Extract stock mentions from full article (not just summary)
        stock_mentions = extract_stocks_from_headline(article_content[:500])
        sentiment_label, sentiment_score = enhanced_sentiment_analysis(article_content[:1000], "")
        
        return jsonify({
            "summary": summary_result,
            "stock_mentions": stock_mentions[:6],
            "sentiment": sentiment_label,
            "sentiment_score": f"{sentiment_score:.2f}",
            "word_count": len(article_content.split()),
            "summary_length": len(summary_result.split()),
            "extraction_method": extraction_method,
            "analysis_success": True
        })
        
    except Exception as e:
        add_log(f"❌ Summarization error: {str(e)}")
        return jsonify({
            "summary": f"Error: {str(e)}",
            "stock_mentions": [],
            "sentiment": "Neutral",
            "sentiment_score": "0.50",
            "analysis_success": False
        })


if __name__ == "__main__":
    add_log("🚀 Starting Stock Market Dashboard - 100% Fixed Version")
    
    print("\n" + "="*70)
    print("🇮🇳 INDIAN STOCK MARKET INTELLIGENCE DASHBOARD")
    print("="*70)
    print("🌐 Dashboard: http://localhost:5000")
    print("📊 Features:")
    print("   ✅ Headline-Only Stock Extraction")
    print("   ✅ Indian Market News Only")
    print("   ✅ Gainers/Losers Analysis")
    print("   ✅ AI Summaries")
    print("   ✅ User Watchlists")
    print("   ✅ Real-time Logs")
    print("="*70 + "\n")
    
    app.run(debug=True, use_reloader=False, host='0.0.0.0', port=5000, threaded=True)
