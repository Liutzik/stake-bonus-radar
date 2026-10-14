# Stake bonus radar

Predicts when Stake's next monthly bonus will drop on stake.com and stake.us.

Every past drop is announced on Stake's official Telegram channels
(t.me/StakeCasino and t.me/StakeSocial). The radar reads their public web
preview, learns which day of the month and time of day drops land on (weighting
recent months and the same month in earlier years), and shows the likely date,
a 50% and 80% window, and a countdown in the viewer's local time.

An hourly GitHub Actions job (`.github/workflows/update.yml`) fetches new
announcements, rebuilds the page and publishes it with GitHub Pages.

    python build_site.py   # build site/ locally

An independent project, not affiliated with or endorsed by Stake.
