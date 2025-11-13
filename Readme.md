
## How This Works

### News Fetching
- Fetches news from 9+ Indian financial RSS feeds
- Multi-threaded processing for fast data aggregation
- Filters only Indian market news from last 50 hours

### Stock Extraction
- Ultra-strict headline parsing extracts stock symbols ONLY from headlines
- Regex-based pattern matching with company name disambiguation
- Cross-references with company.csv containing 2000+ NSE/BSE stocks
- Filters out false positives

### Sector Classification
- 14 sectors: Banking, IT, Oil & Gas, Pharma, Auto, Defense, Metals, FMCG, Real Estate, Cement, Telecom, Power, Retail, Financial Services
- Keyword-based scoring with company name matching
- Sector-specific financial terminology recognition

### Sentiment Analysis
- Rule-based approach using financial keywords
- Positive words: profit, growth, surge, bullish, record
- Negative words: loss, decline, crash, bearish, downgrade
- Sentiment score: 0.0 to 1.0

### Smart Summarization
- Extractive method using financial keyword scoring
- Sentence position weighting
- Number and metric detection
- 3-sentence summaries optimized for readability

### Gainers/Losers Analysis
- Aggregates sentiment by stock per sector
- Ranks stocks by positive/negative mention count
- Displays top 10 gainers and losers per sector
- Links articles to each stock for verification

## User Features

### Authentication
- Secure registration with password hashing
- Session-based login system
- User-specific data isolation

### Personalized Watchlist
- Search and add stocks from 2000+ companies
- Track favorite stocks across all sectors
- View watchlist-specific news and sentiment
- Real-time price updates

## Dashboard Features

### Sector Dashboard
- Tabs for all 14 sectors
- Top gainers with most positive mentions
- Top losers with most negative mentions
- Positive and negative news articles per sector
- Article summaries with sentiment scores

### Real-time Logs
- Live processing logs visible on dashboard
- Track news fetching status
- Monitor article processing count

## Data Sources

**RSS Feeds:**
- Economic Times (Markets, Stocks)
- Moneycontrol (Business, Markets)
- Business Standard (Markets)
- Financial Express (Market)
- Livemint (Markets)
- Zee Business (Markets)
- Google News (Indian Stocks, Sensex)

**Stock Database:**
- company.csv contains Company Name, Stock Symbol, Sector, Industry


