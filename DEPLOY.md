# Publish the static News & Events page

## GitHub Pages: dedicated public project site

Use a dedicated public repository with **`main` as its default branch**. Commit
only the generator, wrapper, dependency lists, tests, documentation and
`.github/workflows/pages.yml`. The workflow runs once daily at 11:23 UTC (6:23
a.m. Eastern in standard time and 7:23 a.m. in daylight time), and manually when
dispatched. It needs no secrets, donor data, private calendars, source
snapshots, or manually curated event input. Do not put any of those in the
repository, workflow logs, or output directory.

1. In the repository, open **Settings → Pages**, then set **Build and
   deployment → Source** to **GitHub Actions**. Do not select `Deploy from a
   branch` or add a personal access token.
2. In **Settings → Environments**, set up or open `github-pages`. Under
   **Deployment branches and tags**, allow the `main` branch only. Restrict who
   can change `main` or workflow files; use approvals for code changes rather
   than making a reviewer manually approve every scheduled deployment.
3. In **Actions → Refresh public News & Events**, choose **Run workflow** on
   `main` for the initial deployment. Check both jobs. Open the verified Pages
   URL reported by the deployment, then open `events.html` relative to that URL.
   For a project repository the URL normally looks like
   `https://OWNER.github.io/REPOSITORY/events.html`, not the owner site's root.
4. From a signed-out browser, confirm that `events.html` returns the listing,
   its date is current, and the live source links work. The workflow deploys
   exactly two files: the listing and a small index linking to it. Local
   examples, demonstration output, source downloads, and manually reviewed
   events are excluded. Each direct GitHub Action is pinned to its official
   release commit. The build can read the repository; only the separate
   deployment job receives Pages and identity permissions.
5. If the parish wants the feed embedded, verify the final HTTPS endpoint and
   that it works in the current Weebly page before changing the live church
   site. Use the iframe and direct-link HTML in the next section, substituting
   the full project URL in both places. GitHub Pages managed hosting does not
   provide the same custom response-header controls as a parish-owned host;
   use the direct link if the embed cannot be verified. No live Weebly edits
   are performed by GitHub Actions.

When a source, test, or validation fails, the workflow does not deploy an
empty or partial listing. The last successful deployment stays available; the
JavaScript-enabled page shows the calendar fallback after 72 hours without a
successful refresh. Watch failed Actions runs and re-run manually after
restoring the source. GitHub Actions scheduled runs can be delayed and may be
disabled after 60 days of repository inactivity. Arrange an independent
freshness check if stale content matters; inspect **Actions** and enable the
schedule again if needed.

Official references: [Custom Pages workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages),
[Deployment environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments),
and [Scheduled workflow triggers](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

# Alternative: a parish-owned HTTPS host

Use a **parish-owned HTTPS host** where a restricted account can run Python,
Poppler and Bash and write one output file. The generator needs no Weebly
password, donor submissions, private calendar, or API keys. Keep the source,
logs and any source snapshots **out of the public directory**. Only the generated
`events.html` needs to be public.

1. Install the documented dependencies and run the unit tests.
2. Run `bash /opt/stg-events/refresh_web.sh /var/www/parish-events/events.html`
   as the account responsible for scheduled refreshes. Confirm the rendered HTML
   without a login, the latest refresh date and the published source links.
3. Schedule one refresh daily (a cron example):

   ```cron
   10 6 * * * PYTHON=/opt/stg-events/.venv/bin/python /usr/bin/bash /opt/stg-events/refresh_web.sh /var/www/parish-events/events.html
   ```

   Configure the host to report nonzero exits. The wrapper leaves the previous
   file intact on failure, and JavaScript-enabled embeds show a calendar
   fallback after 72 hours without a successful refresh. Only add the optional
   manually verified inputs file if someone is responsible for keeping it up to date.
4. Allow the page to be framed by `https://www.saintgeorgeupperdarby.org`
   using a Content-Security-Policy `frame-ancestors` response header. Don't send
   an incompatible X-Frame-Options header for this one endpoint.
5. In Weebly's News & Events current view, add an Embed Code block with the
   verified HTTPS endpoint, plus a normal direct link:

   ```html
   <iframe src="https://PARISH-APPROVED-HOST/events.html"
           title="Upcoming parish and regional events"
           loading="lazy" sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
           style="display:block;width:100%;height:36rem;border:1px solid #e0d1b8"></iframe>
   <p><a href="https://PARISH-APPROVED-HOST/events.html">Open the full event listing</a></p>
   ```

   Verify the public page, and remove any duplicate dated roundup from the main
   view only after the feed is working. Older native Weebly posts and URLs should
   remain in the archive. This design refreshes the visible listing; it does not
   create one new Weebly blog post per event.
