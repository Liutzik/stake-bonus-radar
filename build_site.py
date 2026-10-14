"""Build the public website: fetch new announcements, re-predict, write site/.

Run by the hourly GitHub Actions job (.github/workflows/update.yml). The page
embeds its data, and also re-reads site/data.json every few minutes so a tab
left open picks up new drops without a reload.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import run
import scrape

SITE = Path(__file__).parent / "site"


def main():
    scrape.update_all()
    data = run.page_data(datetime.now(timezone.utc))
    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(run.render(data), "utf-8")
    (SITE / "data.json").write_text(json.dumps(data, separators=(",", ":")), "utf-8")
    print(f"site built: {SITE}")


if __name__ == "__main__":
    main()
