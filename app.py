import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for
from urllib.parse import urlparse, urlunparse, urljoin
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

# --- Database Setup (Rename favicon_url to logo_url) ---
def init_db():
    """Initializes the SQLite database and cache table if they don't exist."""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    # Check if table exists to decide if we need to rename/add columns
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='scrape_cache'")
    table_exists = cursor.fetchone()

    if table_exists:
         # Check if logo_url column exists, if not, check if favicon_url exists to rename
        cursor.execute("PRAGMA table_info(scrape_cache)")
        columns = {info[1]: info for info in cursor.fetchall()} # name: (index, name, type, ...)

        if 'logo_url' not in columns:
            if 'favicon_url' in columns:
                print("Renaming favicon_url column to logo_url in cache.")
                # SQLite doesn't directly support RENAME COLUMN before 3.25.0
                # Safer to add new, copy data, drop old (more complex)
                # Simpler approach for demo: Add logo_url, ignore old favicon_url if present
                try:
                    cursor.execute("ALTER TABLE scrape_cache ADD COLUMN logo_url TEXT")
                    print("Added logo_url column to cache (favicon_url might still exist but won't be used).")
                except sqlite3.OperationalError:
                     pass # Column likely already added in a previous run
            else:
                 try:
                    cursor.execute("ALTER TABLE scrape_cache ADD COLUMN logo_url TEXT")
                    print("Added logo_url column to cache.")
                 except sqlite3.OperationalError:
                     pass # Already exists

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scrape_cache (
            url TEXT PRIMARY KEY,
            scraped_title TEXT,
            scraped_description TEXT,
            scraped_contacts_json TEXT,
            logo_url TEXT,  /* Renamed/Ensured column */
            timestamp REAL
        )
    ''')
    conn.commit()
    conn.close()

# --- Caching Functions (Updated for logo_url) ---
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
            # Make sure logo_url key exists even if null in DB
            logo_url_value = row['logo_url'] if 'logo_url' in row.keys() else None
            return {
                "title": row['scraped_title'],
                "description": row['scraped_description'],
                "contacts": contacts,
                "logo_url": logo_url_value, # Retrieve logo_url
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
        (url, scraped_title, scraped_description, scraped_contacts_json, logo_url, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (url, data.get('title'), data.get('description'), contacts_json, data.get('logo_url'), timestamp)) # Save logo_url
    conn.commit()
    conn.close()

# --- Scraping Functions ---
EMAIL_REGEX = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"

def find_favicon(soup, base_url):
    """Attempts to find the favicon URL."""
    icon_link = soup.find('link', rel=lambda r: r and r.lower() in ['icon', 'shortcut icon'])
    if icon_link and icon_link.get('href'):
        href = icon_link['href'].strip()
        favicon_url = urljoin(base_url, href)
        if favicon_url and not favicon_url.startswith(('data:', 'javascript:')):
             return favicon_url
    return None

def find_logo_url(soup, base_url):
    """Attempts to find a prominent logo URL using heuristics."""
    logo_url = None

    # 1. Try Open Graph image
    og_image = soup.find('meta', property='og:image')
    if og_image and og_image.get('content'):
        og_url = og_image['content'].strip()
        # Basic validation and check if it LOOKS like a logo file
        if og_url and ('logo' in og_url.lower() or 'brand' in og_url.lower()):
             logo_url = urljoin(base_url, og_url)
             if logo_url and not logo_url.startswith(('data:', 'javascript:')): return logo_url
             logo_url = None # Reset if invalid

    # 2. Try common selectors/attributes for <img> tags
    selectors = [
        'img[id*="logo" i]', 'img[class*="logo" i]',
        'header img', 'nav img', 'a[href="/"] img', 'a[class*="brand" i] img',
        'img[alt*="logo" i]', 'img[src*="logo"]'
    ]
    for selector in selectors:
        try:
            img_tag = soup.select_one(selector)
            if img_tag and img_tag.get('src'):
                src = img_tag['src'].strip()
                # Basic check for tiny images (likely icons, spacers) or invalid types
                width = img_tag.get('width', '100')
                height = img_tag.get('height', '100')
                try: # Handle non-numeric width/height
                    if int(width) < 20 or int(height) < 20: continue
                except ValueError: pass
                if src.lower().endswith(('.gif', '.svg', '.png', '.jpg', '.jpeg', '.webp')) and not src.startswith(('data:', 'javascript:')):
                    logo_url = urljoin(base_url, src)
                    if logo_url: return logo_url # Return first plausible match
        except Exception:
            continue # Ignore errors from invalid selectors etc.

    # 3. Fallback (already handled in scrape_website) - Favicon
    return None # Explicitly return None if no logo found by heuristics


def scrape_website(url):
    """
    Scrapes the website for title, meta description, contacts, logo, and favicon (as fallback logo).
    """
    headers = {'User-Agent': USER_AGENT}
    logo_url = None # Initialize
    try:
        response = requests.get(url, headers=headers, timeout=10, verify=True)
        response.raise_for_status()
        response.encoding = response.apparent_encoding
        html_content = response.text
        soup = BeautifulSoup(html_content, 'html.parser')

        title = soup.title.string.strip() if soup.title else None
        description_tag = soup.find('meta', attrs={'name': 'description'})
        description = description_tag['content'].strip() if description_tag and 'content' in description_tag.attrs else None

        # Try finding logo first
        logo_url = find_logo_url(soup, url)
        # If no logo found, try favicon as fallback
        if not logo_url:
            logo_url = find_favicon(soup, url)

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
            "logo_url": logo_url, # Use the found logo/favicon URL
        }
    except requests.exceptions.Timeout: return {"error": f"Request timed out."}
    except requests.exceptions.SSLError as e: return {"error": f"SSL Error: {e}"}
    except requests.exceptions.RequestException as e: return {"error": f"Request Error: {e}"}
    except Exception as e: return {"error": f"Parsing Error: {e}"}


# --- Cyrillic Domain Generation (Unchanged) ---
CYRILLIC_MAP = { 'a': 'а', 'c': 'с', 'e': 'е', 'o': 'о', 'p': 'р', 'x': 'х', 'y': 'у', 'k': 'к', 'm': 'м', 't': 'т',}
LATIN_CHARS_TO_REPLACE = list(CYRILLIC_MAP.keys())
SUPPORTED_TLDS = ['com', 'org', 'net', 'info', 'biz', 'xyz', 'online', 'site', 'icu', 'live', 'club']
def generate_cyrillic_domains(domain):
    suggestions = []; unique_domains = set()
    if not domain or '.' not in domain: return []
    name, _, tld = domain.partition('.');
    if not name: return []
    replaceable_indices = [i for i, char in enumerate(name) if char in LATIN_CHARS_TO_REPLACE]
    if not replaceable_indices: return []
    for index in replaceable_indices:
        original_char = name[index]; cyrillic_char = CYRILLIC_MAP[original_char]
        new_name_list = list(name); new_name_list[index] = cyrillic_char; new_name = "".join(new_name_list)
        if new_name != name:
            note = f"Original '{original_char}' at pos {index + 1} replaced with Cyrillic '{cyrillic_char}'."
            num_tlds_to_sample = min(len(SUPPORTED_TLDS), 2); tlds_to_try = random.sample(SUPPORTED_TLDS, num_tlds_to_sample)
            if tld in SUPPORTED_TLDS and tld not in tlds_to_try: tlds_to_try.append(tld)
            if len(tlds_to_try) > num_tlds_to_sample + 1 : random.shuffle(tlds_to_try); tlds_to_try = tlds_to_try[:num_tlds_to_sample+1]
            for target_tld in tlds_to_try:
                full_domain = f"{new_name}.{target_tld}"
                if full_domain not in unique_domains: suggestions.append({"domain": full_domain, "note": note}); unique_domains.add(full_domain)
    random.shuffle(suggestions); return suggestions[:8]

# --- Flask Routes ---
@app.route('/')
def index():
    return render_template('index.html') # Assumes index.html exists

@app.route('/analyze', methods=['POST'])
def analyze():
    url = request.form.get('url')
    if not url: return redirect(url_for('index'))
    parsed_url = urlparse(url)
    if not parsed_url.scheme: url = 'https://' + url; parsed_url = urlparse(url)
    if not parsed_url.netloc: return render_template('results.html', url=url, error="Invalid URL provided.")

    clean_url = urlunparse(parsed_url._replace(path='', params='', query='', fragment=''))
    domain = parsed_url.netloc.replace('www.', '')

    cache_hit = False; cached_data = get_cached_data(clean_url); scraped_data = None; error = None; logo_url_found = None # Renamed variable
    cache_age_str = "N/A (Live)"

    if cached_data:
        scraped_data = cached_data
        logo_url_found = scraped_data.get('logo_url') # Get logo_url from cache
        cache_hit = True
        cache_age_seconds = cached_data.pop('cache_age_seconds', 0)
        if cache_age_seconds < 60: cache_age_str = f"{cache_age_seconds} sec"
        elif cache_age_seconds < 3600: cache_age_str = f"{cache_age_seconds // 60} min"
        else: cache_age_str = f"{cache_age_seconds // 3600} hr"
    else:
        scraped_data = scrape_website(clean_url)
        error = scraped_data.get("error")
        if not error:
            logo_url_found = scraped_data.get('logo_url') # Get logo_url from scrape
            save_to_cache(clean_url, scraped_data)

    cyrillic_domains = []
    if not error:
        scraped_data = scraped_data or {} # Ensure dict
        cyrillic_domains = generate_cyrillic_domains(domain)

    current_year = datetime.datetime.now().year

    # Pass logo_url to template
    return render_template('results.html',
                           url=clean_url,
                           domain=domain,
                           scraped_data=scraped_data,
                           error=error,
                           cyrillic_domains=cyrillic_domains,
                           current_year=current_year,
                           cache_hit=cache_hit,
                           cache_age=cache_age_str,
                           logo_url=logo_url_found # Pass the found logo/favicon URL
                           )

# --- Main Execution ---
if __name__ == '__main__':
    init_db()
    print(f"Starting Flask app - navigate to http://127.0.0.1:5001")
    app.run(debug=False, host='127.0.0.1', port=8090)