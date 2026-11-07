"""
AKU-EB Various Forms PDF Downloader
URL: https://examinationboard.aku.edu/services/Pages/various-forms.aspx
- Selenium based (handles JS/SharePoint)
- Auto-detects pagination (numbers, Next button, both)
- Downloads all 28 PDFs across 4 pages
- Retry logic, duplicate skip, full logging
"""

import os
import re
import json
import time
import logging
import hashlib
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
import urllib3
urllib3.disable_warnings()

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, ElementClickInterceptedException
)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TARGET_URL   = "https://examinationboard.aku.edu/services/Pages/various-forms.aspx"
OUTPUT_DIR   = "downloaded_forms"
LOG_DIR      = "logs_forms"
TOTAL_PAGES  = 4
WAIT_AFTER_CLICK = 3      # seconds after clicking pagination
PAGE_LOAD_WAIT   = 15     # seconds max wait for elements
DOWNLOAD_RETRIES = 3

# ─── LOGGING ─────────────────────────────────────────────────────────────────
Path(LOG_DIR).mkdir(exist_ok=True)
Path(OUTPUT_DIR).mkdir(exist_ok=True)

log_path = f"{LOG_DIR}/forms_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[logging.FileHandler(log_path), logging.StreamHandler()]
)
log = logging.getLogger()

# ─── SELENIUM SETUP ───────────────────────────────────────────────────────────
def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    # Try to find chromedriver automatically
    try:
        driver = webdriver.Chrome(options=opts)
    except Exception:
        # Try explicit path (common locations)
        for path in ["/usr/bin/chromedriver", "/usr/local/bin/chromedriver",
                     "chromedriver", "chromedriver.exe"]:
            try:
                driver = webdriver.Chrome(service=Service(path), options=opts)
                break
            except Exception:
                continue
        else:
            raise RuntimeError("chromedriver not found! Install: pip install webdriver-manager  OR  apt install chromium-driver")
    return driver


# ─── WAIT HELPER ─────────────────────────────────────────────────────────────
def wait_for_content(driver, timeout=PAGE_LOAD_WAIT):
    """Wait until at least one PDF link OR a 'no results' message appears."""
    wait = WebDriverWait(driver, timeout)
    try:
        wait.until(lambda d: (
            len(d.find_elements(By.XPATH, "//a[contains(@href,'.pdf') or contains(@href,'.PDF')]")) > 0
            or len(d.find_elements(By.XPATH, "//*[contains(text(),'no') and contains(text(),'form')]")) > 0
            or len(d.find_elements(By.CSS_SELECTOR, "table td")) > 0
        ))
    except TimeoutException:
        log.warning("Timed out waiting for content – proceeding anyway")
    time.sleep(1.5)   # small buffer after DOM settles


# ─── EXTRACT PDF LINKS FROM CURRENT PAGE ─────────────────────────────────────
def extract_links(driver):
    """Return list of {url, title} dicts for every PDF on current DOM."""
    results = []
    seen = set()

    # Strategy 1: direct <a href="*.pdf">
    anchors = driver.find_elements(By.XPATH,
        "//a[contains(translate(@href,'PDF','pdf'),'.pdf')]"
    )
    for a in anchors:
        href = a.get_attribute("href") or ""
        if href and ".pdf" in href.lower() and href not in seen:
            seen.add(href)
            title = a.text.strip() or href.split("/")[-1]
            results.append({"url": href, "title": title})

    # Strategy 2: onclick / data-url attributes that hide real URL
    for el in driver.find_elements(By.XPATH, "//*[@onclick or @data-url]"):
        for attr in ["onclick", "data-url", "data-href"]:
            val = el.get_attribute(attr) or ""
            m = re.search(r"(https?://[^\s'\"]+\.pdf)", val, re.I)
            if m:
                href = m.group(1)
                if href not in seen:
                    seen.add(href)
                    results.append({"url": href, "title": el.text.strip() or href.split("/")[-1]})

    log.info(f"  Found {len(results)} PDF link(s) on this page view")
    return results


# ─── PAGINATION ───────────────────────────────────────────────────────────────
def click_page(driver, page_num):
    """
    Try every known pagination pattern to reach page_num.
    Returns True if click succeeded.
    """
    wait = WebDriverWait(driver, 8)

    strategies = [
        # numbered link with exact text
        (By.XPATH, f"//a[normalize-space(text())='{page_num}']"),
        # span-wrapped number inside anchor
        (By.XPATH, f"//a[.//span[normalize-space(text())='{page_num}']]"),
        # li > a pattern (Bootstrap/custom pagination)
        (By.XPATH, f"//li/a[normalize-space(text())='{page_num}']"),
        # any element with page number as text, inside a pagination div
        (By.XPATH, f"//*[contains(@class,'pag')]//*[normalize-space(text())='{page_num}']"),
        # SharePoint-style: PageNum
        (By.XPATH, f"//*[@data-page='{page_num}']"),
        # Next button (only useful for sequential navigation)
        (By.XPATH, "//a[contains(translate(text(),'NEXT','next'),'next')]"),
        (By.XPATH, "//a[contains(@class,'next')]"),
        (By.XPATH, "//*[contains(@class,'next-page')]"),
        # SharePoint pager
        (By.XPATH, "//td[@class='ms-paging']//a"),
    ]

    for by, sel in strategies:
        try:
            els = driver.find_elements(by, sel)
            if not els:
                continue
            el = els[0]
            driver.execute_script("arguments[0].scrollIntoView(true);", el)
            time.sleep(0.3)
            try:
                el.click()
            except ElementClickInterceptedException:
                driver.execute_script("arguments[0].click();", el)
            log.info(f"  Clicked page {page_num} via: {sel}")
            time.sleep(WAIT_AFTER_CLICK)
            return True
        except Exception:
            continue

    # Last resort: dump all clickable text to help debug
    clickable = driver.find_elements(By.XPATH, "//a | //button")
    log.warning(f"  Could not find page {page_num} button. Visible links:")
    for c in clickable[-30:]:
        t = c.text.strip()
        h = c.get_attribute("href") or ""
        if t or h:
            log.warning(f"    '{t}'  href={h[:60]}")
    return False


# ─── DOWNLOAD ONE PDF ─────────────────────────────────────────────────────────
def download_pdf(url, filename, session, output_dir, retries=DOWNLOAD_RETRIES):
    filepath = Path(output_dir) / filename
    if filepath.exists():
        log.info(f"  ✓ Skip (exists): {filename}")
        return True

    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=60, stream=True, verify=False)
            if r.status_code == 200:
                content = b"".join(r.iter_content(8192))
                if len(content) < 200:
                    log.warning(f"  ⚠ Too small ({len(content)} B) – {filename}")
                    break
                if not content[:4].startswith(b'%PDF'):
                    log.warning(f"  ⚠ Not a PDF header – saving anyway: {filename}")
                filepath.write_bytes(content)
                log.info(f"  ✓ Downloaded [{len(content)/1024:.1f} KB]: {filename}")
                return True
            else:
                log.warning(f"  ⚠ HTTP {r.status_code} attempt {attempt}/{retries}: {filename}")
        except Exception as e:
            log.warning(f"  ⚠ Error attempt {attempt}/{retries}: {e}")
        time.sleep(2 ** (attempt - 1))

    log.error(f"  ✗ FAILED: {filename}")
    return False


# ─── SAFE FILENAME ────────────────────────────────────────────────────────────
def safe_name(title, url, idx, used):
    name = title if title else url.split("/")[-1].split("?")[0]
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip()
    name = re.sub(r"_+", "_", name)[:80]
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    # ensure uniqueness
    base, ext = os.path.splitext(name)
    candidate, n = name, idx
    while candidate in used:
        candidate = f"{base}_{n}{ext}"
        n += 1
    used.add(candidate)
    return candidate


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("  AKU-EB Forms PDF Downloader  –  Starting")
    log.info(f"  URL        : {TARGET_URL}")
    log.info(f"  Output     : {Path(OUTPUT_DIR).resolve()}")
    log.info("=" * 65)

    driver = make_driver()
    all_links = []   # {url, title}
    seen_urls = set()

    try:
        log.info("Loading page …")
        driver.get(TARGET_URL)
        wait_for_content(driver)

        # Dump full page source once for debugging
        with open(f"{LOG_DIR}/page1_source.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
        log.info("Page 1 source saved to logs_forms/page1_source.html")

        for pg in range(1, TOTAL_PAGES + 1):
            log.info(f"\n{'─'*50}")
            log.info(f"  PAGE {pg} / {TOTAL_PAGES}")
            log.info(f"{'─'*50}")

            if pg > 1:
                ok = click_page(driver, pg)
                if not ok:
                    log.warning(f"  Skipping page {pg} – could not navigate")
                    continue
                wait_for_content(driver)

            for item in extract_links(driver):
                if item["url"] not in seen_urls:
                    seen_urls.add(item["url"])
                    all_links.append(item)

        log.info(f"\n{'='*65}")
        log.info(f"  Total unique PDFs collected: {len(all_links)}")
        log.info(f"{'='*65}")
        for i, l in enumerate(all_links, 1):
            log.info(f"  {i:02d}. {l['title'][:55]}  →  {l['url'][:60]}")

    finally:
        driver.quit()

    if not all_links:
        log.error("\nNo PDFs found! Check logs_forms/page1_source.html to inspect page structure.")
        log.error("Share that file and I'll fix the selectors.")
        return

    # ── Download phase ────────────────────────────────────────────────────────
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": TARGET_URL,
    })

    used_names = set()
    ok_count = fail_count = 0
    failed = []

    for i, item in enumerate(all_links, 1):
        fname = safe_name(item["title"], item["url"], i, used_names)
        log.info(f"\n[{i}/{len(all_links)}] {fname}")
        if download_pdf(item["url"], fname, session, OUTPUT_DIR):
            ok_count += 1
        else:
            fail_count += 1
            failed.append({"filename": fname, "url": item["url"]})

    # ── Report ────────────────────────────────────────────────────────────────
    report = {
        "timestamp": datetime.now().isoformat(),
        "source_url": TARGET_URL,
        "total_found": len(all_links),
        "downloaded": ok_count,
        "failed": fail_count,
        "failed_files": failed,
    }
    rpath = Path(OUTPUT_DIR) / "report.json"
    rpath.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    log.info(f"\n{'='*65}")
    log.info(f"  ✓  Downloaded : {ok_count}")
    log.info(f"  ✗  Failed     : {fail_count}")
    log.info(f"  PDFs folder   : {Path(OUTPUT_DIR).resolve()}")
    log.info(f"  Log file      : {Path(log_path).resolve()}")
    log.info(f"  Report        : {rpath.resolve()}")
    log.info(f"{'='*65}")


if __name__ == "__main__":
    print("\n" + "=" * 65)
    print("  AKU-EB Forms PDF Downloader")
    print("=" * 65)
    print("\nRequired (one-time install):")
    print("  pip install selenium requests beautifulsoup4")
    print("  # Chrome + chromedriver must be installed")
    print("  # OR: pip install webdriver-manager  (auto-installs driver)")
    print("\n  If chromedriver not found, add this at top of script:")
    print("  from webdriver_manager.chrome import ChromeDriverManager")
    print("  service = Service(ChromeDriverManager().install())")
    print("\nRunning …\n")
    main()
