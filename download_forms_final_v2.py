"""
AKU-EB Various Forms PDF Downloader  –  v2 (webdriver-manager edition)
Auto-installs chromedriver. Just run: python download_forms_final_v2.py
"""

# ── auto-install dependencies if missing ─────────────────────────────────────
import subprocess, sys
for pkg in ["selenium", "webdriver_manager", "requests", "urllib3"]:
    try:
        __import__(pkg)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

import os, re, json, time, logging
from pathlib import Path
from datetime import datetime

import requests, urllib3
urllib3.disable_warnings()

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException
from webdriver_manager.chrome import ChromeDriverManager

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TARGET_URL      = "https://examinationboard.aku.edu/services/Pages/various-forms.aspx"
OUTPUT_DIR      = "downloaded_forms"
LOG_DIR         = "logs_forms"
TOTAL_PAGES     = 4
WAIT_CONTENT    = 12   # seconds to wait for JS content load
WAIT_PAGINATION = 4    # seconds after pagination click

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

# ─── DRIVER ──────────────────────────────────────────────────────────────────
def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)

# ─── WAIT FOR PAGE CONTENT ────────────────────────────────────────────────────
def wait_for_content(driver):
    deadline = time.time() + WAIT_CONTENT
    while time.time() < deadline:
        pdfs = driver.find_elements(By.XPATH,
            "//a[contains(translate(@href,'PDF','pdf'),'.pdf')]")
        cells = driver.find_elements(By.CSS_SELECTOR, "table td, div.ms-rtestate-field")
        if pdfs or cells:
            break
        time.sleep(1)
    time.sleep(1.5)  # let JS settle

# ─── EXTRACT PDFS FROM CURRENT PAGE ─────────────────────────────────────────
def extract_links(driver):
    found, seen = [], set()

    # Grab ALL anchor tags and filter .pdf
    for a in driver.find_elements(By.TAG_NAME, "a"):
        href = (a.get_attribute("href") or "").strip()
        if ".pdf" in href.lower() and href not in seen:
            seen.add(href)
            title = a.text.strip() or href.split("/")[-1].split("?")[0]
            found.append({"url": href, "title": title})
            log.info(f"    PDF: {title[:60]}  →  {href}")

    # Also scan onclick / data-* for hidden PDF references
    for el in driver.find_elements(By.XPATH, "//*[@onclick]"):
        val = el.get_attribute("onclick") or ""
        for m in re.finditer(r"(https?://\S+\.pdf)", val, re.I):
            href = m.group(1).strip("'\"")
            if href not in seen:
                seen.add(href)
                found.append({"url": href, "title": el.text.strip() or href.split("/")[-1]})

    log.info(f"  → {len(found)} unique PDF(s) on this page")
    return found

# ─── CLICK PAGINATION ────────────────────────────────────────────────────────
def go_to_page(driver, pg):
    if pg == 1:
        return True

    # All realistic selectors for SharePoint/custom pagination
    xpaths = [
        f"//a[normalize-space(text())='{pg}']",
        f"//span[normalize-space(text())='{pg}']/parent::a",
        f"//li[not(contains(@class,'disabled'))]/a[normalize-space(text())='{pg}']",
        f"//*[contains(@class,'pager') or contains(@class,'pagination')]//a[contains(text(),'{pg}')]",
        f"//*[@data-page='{pg}']",
        f"//td[contains(@class,'ms-paging')]//a[contains(text(),'{pg}')]",
        # "Next" arrow – only if numbered didn't work
        "//a[translate(normalize-space(text()),'NEXT >»','next >»')='next' or @title='Next Page']",
        "//a[contains(@class,'next') and not(contains(@class,'disabled'))]",
    ]

    for xp in xpaths:
        els = driver.find_elements(By.XPATH, xp)
        for el in els:
            try:
                if not el.is_displayed():
                    continue
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.3)
                try:
                    el.click()
                except ElementClickInterceptedException:
                    driver.execute_script("arguments[0].click();", el)
                log.info(f"  Paginated to page {pg}  (xpath: {xp[:60]})")
                time.sleep(WAIT_PAGINATION)
                return True
            except Exception:
                continue

    # Debug dump
    log.warning(f"  ⚠ Could not click page {pg}. All anchors visible:")
    for a in driver.find_elements(By.TAG_NAME, "a"):
        t = a.text.strip()
        h = a.get_attribute("href") or ""
        if t.isdigit() or t.lower() in ("next","previous","»","«",">","<"):
            log.warning(f"    '{t}'  href={h[:70]}")
    return False

# ─── DOWNLOAD PDF ─────────────────────────────────────────────────────────────
def download_pdf(url, fname, sess):
    fpath = Path(OUTPUT_DIR) / fname
    if fpath.exists():
        log.info(f"  ✓ Already exists – skip: {fname}")
        return True
    for attempt in range(1, 4):
        try:
            r = sess.get(url, timeout=60, stream=True, verify=False)
            if r.status_code == 200:
                data = b"".join(r.iter_content(8192))
                if len(data) < 100:
                    log.warning(f"  ⚠ Suspiciously small ({len(data)}B): {fname}")
                    return False
                fpath.write_bytes(data)
                log.info(f"  ✓ Saved [{len(data)/1024:.1f} KB]: {fname}")
                return True
            else:
                log.warning(f"  ⚠ HTTP {r.status_code} attempt {attempt}: {fname}")
        except Exception as e:
            log.warning(f"  ⚠ Attempt {attempt} error: {e}")
        time.sleep(2 ** (attempt - 1))
    log.error(f"  ✗ FAILED after 3 tries: {fname}")
    return False

# ─── FILENAME HELPER ─────────────────────────────────────────────────────────
def safe_fname(title, url, idx, used):
    name = title if title else url.split("/")[-1].split("?")[0]
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip()[:80]
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    base, ext = os.path.splitext(name)
    out, n = name, idx
    while out in used:
        out = f"{base}_{n}{ext}"; n += 1
    used.add(out)
    return out

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("  AKU-EB Forms Downloader  —  v2")
    log.info(f"  {TARGET_URL}")
    log.info("=" * 65)

    driver = make_driver()
    all_links, seen_urls = [], set()

    try:
        log.info("Opening browser …")
        driver.get(TARGET_URL)
        wait_for_content(driver)

        # Save page 1 source for manual inspection if needed
        src_path = Path(LOG_DIR) / "page1_source.html"
        src_path.write_text(driver.page_source, encoding="utf-8")
        log.info(f"Page source saved → {src_path}")

        for pg in range(1, TOTAL_PAGES + 1):
            log.info(f"\n{'─'*55}")
            log.info(f"  Scraping page {pg}/{TOTAL_PAGES}")
            log.info(f"{'─'*55}")

            if pg > 1:
                if not go_to_page(driver, pg):
                    log.warning(f"  Skipping page {pg}")
                    continue
                wait_for_content(driver)

            for item in extract_links(driver):
                if item["url"] not in seen_urls:
                    seen_urls.add(item["url"])
                    all_links.append(item)

    finally:
        driver.quit()

    log.info(f"\n{'='*65}")
    log.info(f"  Total unique PDFs found: {len(all_links)}")
    log.info(f"{'='*65}")

    if not all_links:
        log.error("No PDFs found!")
        log.error(f"Open {LOG_DIR}/page1_source.html in browser to inspect page structure.")
        return

    # ── Download ──────────────────────────────────────────────────────────────
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": TARGET_URL,
    })

    used, ok, fail, failed_list = set(), 0, 0, []
    for i, item in enumerate(all_links, 1):
        fname = safe_fname(item["title"], item["url"], i, used)
        log.info(f"\n[{i:02d}/{len(all_links):02d}] {fname}")
        if download_pdf(item["url"], fname, sess):
            ok += 1
        else:
            fail += 1
            failed_list.append({"file": fname, "url": item["url"]})

    # ── Summary ───────────────────────────────────────────────────────────────
    report = {
        "timestamp": datetime.now().isoformat(),
        "url": TARGET_URL,
        "found": len(all_links),
        "downloaded": ok,
        "failed": fail,
        "failed_list": failed_list,
    }
    rfile = Path(OUTPUT_DIR) / "report.json"
    rfile.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    log.info(f"\n{'='*65}")
    log.info(f"  ✓  Downloaded : {ok} / {len(all_links)}")
    log.info(f"  ✗  Failed     : {fail}")
    log.info(f"  Folder        : {Path(OUTPUT_DIR).resolve()}")
    log.info(f"  Log           : {Path(log_path).resolve()}")
    log.info(f"{'='*65}")

if __name__ == "__main__":
    main()
