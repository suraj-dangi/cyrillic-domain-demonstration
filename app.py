import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for
from urllib.parse import urlparse, urlunparse, urljoin # Added urljoin
import datetime
import sqlite3
import json
import time
import random

app = Flask(__name__)

# --- Constants ---
DATABASE_FILE = 'phishing_cache.db'
CACHE_DURATION_SECONDS = 3600 * 1 # Cache for 1 hour
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'

# --- Database Setup ---
def init_db():
    """Initializes the SQLite database and cache table if they don't exist."""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    # Add favicon_url column
    try:
        cursor.execute("ALTER TABLE scrape_cache ADD COLUMN favicon_url TEXT")
        print("Added favicon_url column to cache.")
    except sqlite3.OperationalError:
        # Column likely already exists, ignore the error
        pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scrape_cache (
            url TEXT PRIMARY KEY,
            scraped_title TEXT,
            scraped_description TEXT,
            scraped_contacts_json TEXT,
            favicon_url TEXT,
            timestamp REAL
        )
    ''')
    conn.commit()
    conn.close()

# --- Caching Functions (Updated for favicon) ---
def get_cached_data(url):
    """Retrieves cached data for a URL if it's within the cache duration."""
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM scrape_cache WHERE url = ?", (url,))
    row = cursor.fetchone()
    conn.close()

    if row:
        cache_timestamp = row['timestamp']
        current_time = time.time()
        if current_time - cache_timestamp < CACHE_DURATION_SECONDS:
            contacts = json.loads(row['scraped_contacts_json']) if row['scraped_contacts_json'] else []
            return {
                "title": row['scraped_title'],
                "description": row['scraped_description'],
                "contacts": contacts,
                "favicon_url": row['favicon_url'], # Retrieve favicon
                "timestamp": datetime.datetime.fromtimestamp(cache_timestamp).strftime('%Y-%m-%d %H:%M:%S'),
                "cache_age_seconds": int(current_time - cache_timestamp)
            }
    return None

def save_to_cache(url, data):
    """Saves scraped data to the cache."""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    contacts_json = json.dumps(data.get('contacts', []))
    timestamp = time.time()
    cursor.execute('''
        INSERT OR REPLACE INTO scrape_cache
        (url, scraped_title, scraped_description, scraped_contacts_json, favicon_url, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (url, data.get('title'), data.get('description'), contacts_json, data.get('favicon_url'), timestamp)) # Save favicon
    conn.commit()
    conn.close()

# --- Scraping Function (Updated for Favicon) ---
EMAIL_REGEX = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"

def find_favicon(soup, base_url):
    """Attempts to find the favicon URL."""
    # Look for standard link rel tags
    icon_link = soup.find('link', rel=lambda r: r and r.lower() in ['icon', 'shortcut icon'])
    if icon_link and icon_link.get('href'):
        href = icon_link['href'].strip()
        # Resolve relative URLs
        favicon_url = urljoin(base_url, href)
        return favicon_url
    else:
        # Fallback: Check for default /favicon.ico
        # Let's try fetching it to see if it exists (can add latency)
        # For simplicity here, we'll just construct the default path
        # A more robust solution might make a HEAD request
        default_favicon_path = urljoin(base_url, '/favicon.ico')
        # You could optionally add a check here if default_favicon_path actually returns 200 OK
        # For this demo, we'll assume it *might* exist if no link tag found.
        # Consider adding logic to verify existence if needed.
        # Let's only return if explicitly found in link tags for reliability in demo
        return None # Return None if not found via link tags


def scrape_website(url):
    """
    Scrapes the website for title, meta description, contacts, and favicon.
    """
    headers = {'User-Agent': USER_AGENT}
    try:
        response = requests.get(url, headers=headers, timeout=10, verify=True)
        response.raise_for_status()
        response.encoding = response.apparent_encoding
        html_content = response.text

        soup = BeautifulSoup(html_content, 'html.parser')

        title = soup.title.string.strip() if soup.title else None
        description_tag = soup.find('meta', attrs={'name': 'description'})
        description = description_tag['content'].strip() if description_tag and 'content' in description_tag.attrs else None

        # Find Favicon
        favicon_url = find_favicon(soup, url)

        contacts = set()
        body_text = soup.get_text(separator=' ')
        found_emails = re.findall(EMAIL_REGEX, body_text)
        for email in found_emails:
            if not any(ext in email.lower() for ext in ['.png', '.jpg', '.gif', '.css', '.js']):
                 contacts.add(email)

        return {
            "title": title,
            "description": description,
            "contacts": list(contacts),
            "favicon_url": favicon_url, # Include favicon
        }

    except requests.exceptions.Timeout:
        return {"error": f"Request timed out after 10 seconds."}
    except requests.exceptions.SSLError as e:
         return {"error": f"SSL Error connecting to {url}. Check the certificate or try http:// if applicable. Error: {e}"}
    except requests.exceptions.RequestException as e:
        return {"error": f"Error fetching URL: {e}"}
    except Exception as e:
        return {"error": f"Error parsing content: {e}"}


# --- Cyrillic Domain Generation (Generating full domains like name.tld) ---
CYRILLIC_MAP = {
    'a': 'а', 'c': 'с', 'e': 'е', 'o': 'о',
    'p': 'р', 'x': 'х', 'y': 'у', 'k': 'к', 'm': 'м', 't': 'т',
}
LATIN_CHARS_TO_REPLACE = list(CYRILLIC_MAP.keys())
# TLDs known to generally support Internationalized Domain Names (IDN)
SUPPORTED_TLDS = ['com', 'org', 'net', 'info', 'biz', 'xyz', 'online', 'site', 'icu', 'live', 'club']

def generate_cyrillic_domains(domain):
    """Generates potential IDN homograph full domains (name + TLD) using Cyrillic characters."""
    suggestions = [] # List to hold {'domain': ..., 'note': ...} dictionaries
    unique_domains = set() # Keep track of domains added to avoid duplicates

    if not domain or '.' not in domain:
        return []

    name, _, tld = domain.partition('.')
    if not name:
        return []

    replaceable_indices = [i for i, char in enumerate(name) if char in LATIN_CHARS_TO_REPLACE]

    if not replaceable_indices:
        return []

    # Generate domains replacing ONE character
    for index in replaceable_indices:
        original_char = name[index]
        cyrillic_char = CYRILLIC_MAP[original_char]
        new_name_list = list(name)
        new_name_list[index] = cyrillic_char
        new_name = "".join(new_name_list)

        # Ensure the generated name is different from the original
        if new_name != name:
            note = f"Original '{original_char}' at position {index + 1} replaced with Cyrillic '{cyrillic_char}'."

            # Add with a couple of different TLDs for variety
            # Ensure we try at least 2 different TLDs if possible
            num_tlds_to_sample = min(len(SUPPORTED_TLDS), 2)
            tlds_to_try = random.sample(SUPPORTED_TLDS, num_tlds_to_sample)

            # Also include the original TLD if it's supported and not already sampled
            if tld in SUPPORTED_TLDS and tld not in tlds_to_try:
                tlds_to_try.append(tld)
                # If adding the original made us exceed 2, trim back randomly
                if len(tlds_to_try) > num_tlds_to_sample + 1 : # allow max 3 total usually
                     random.shuffle(tlds_to_try)
                     tlds_to_try = tlds_to_try[:num_tlds_to_sample+1]


            for target_tld in tlds_to_try:
                # Construct the full domain (e.g., threaтdefence.com)
                full_domain = f"{new_name}.{target_tld}"

                # Add if unique
                if full_domain not in unique_domains:
                    suggestions.append({"domain": full_domain, "note": note})
                    unique_domains.add(full_domain)

    random.shuffle(suggestions)
    # Limit the total number of examples shown
    return suggestions[:8]

# Make sure the /analyze route still calls generate_cyrillic_domains(domain)
# and passes the result as cyrillic_domains to the template.


# --- Flask Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/analyze', methods=['POST'])
def analyze():
    url = request.form.get('url')
    if not url:
        return redirect(url_for('index'))

    parsed_url = urlparse(url)
    if not parsed_url.scheme:
        url = 'https://' + url
        parsed_url = urlparse(url)
    if not parsed_url.netloc:
        return render_template('results.html', url=url, error="Invalid URL provided.")

    # Use scheme + netloc as the canonical URL for scraping and caching
    clean_url = urlunparse(parsed_url._replace(path='', params='', query='', fragment=''))
    domain = parsed_url.netloc.replace('www.', '')

    cache_hit = False
    cache_info = None
    cached_data = get_cached_data(clean_url)
    scraped_data = None
    error = None
    favicon_url = None # Initialize favicon_url

    if cached_data:
        scraped_data = cached_data
        favicon_url = scraped_data.get('favicon_url') # Get from cache
        error = None
        cache_hit = True
        cache_age_seconds = cached_data.pop('cache_age_seconds', 0)
        cache_timestamp = cached_data.pop('timestamp', 'N/A')
        if cache_age_seconds < 60: cache_age_str = f"{cache_age_seconds} sec"
        elif cache_age_seconds < 3600: cache_age_str = f"{cache_age_seconds // 60} min"
        else: cache_age_str = f"{cache_age_seconds // 3600} hr"
        # cache_info_for_template = {**scraped_data, "url": clean_url, "timestamp": cache_timestamp} # Removed for cleaner UI
    else:
        scraped_data = scrape_website(clean_url)
        error = scraped_data.get("error")
        if not error:
            favicon_url = scraped_data.get('favicon_url') # Get from scrape
            save_to_cache(clean_url, scraped_data)
        cache_age_str = "N/A (Live)"
        # cache_info_for_template = None

    cyrillic_domains = []
    if not error:
        scraped_data = scraped_data or {}
        cyrillic_domains = generate_cyrillic_domains(domain)

    current_year = datetime.datetime.now().year

    return render_template('results.html',
                           url=clean_url,
                           domain=domain,
                           scraped_data=scraped_data,
                           error=error,
                           cyrillic_domains=cyrillic_domains,
                           current_year=current_year,
                           cache_hit=cache_hit,
                           cache_age=cache_age_str,
                           favicon_url=favicon_url, # Pass favicon url to template
                           # cache_info=cache_info_for_template # Removed for cleaner UI
                           )

# --- Main Execution ---
if __name__ == '__main__':
    init_db()
    app.run(debug=False, host='0.0.0.0', port=8090)