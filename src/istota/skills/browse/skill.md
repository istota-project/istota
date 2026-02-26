# Web Browsing

Headless browser for fetching pages that need JavaScript rendering or bot detection bypass. For simple static pages or APIs, prefer `curl` or `httpx` — they're faster.

## Commands

```bash
# Fetch a page
python -m istota.skills.browse get "https://example.com"
python -m istota.skills.browse get "https://example.com" --keep-session --timeout 60
python -m istota.skills.browse get "https://example.com" --wait-for "article.content"

# Navigate within an existing session (preserves cookies, referrer, state)
python -m istota.skills.browse get "https://example.com/page2" --session <id>

# Fetch only links (no page text)
python -m istota.skills.browse links "https://example.com"
python -m istota.skills.browse links "https://example.com" --selector "nav a"

# Screenshot
python -m istota.skills.browse screenshot "https://example.com" -o /tmp/page.png
python -m istota.skills.browse screenshot --session <id> -o /tmp/page.png --full-page

# Extract by CSS selector
python -m istota.skills.browse extract "https://example.com" -s "article"
python -m istota.skills.browse extract --session <id> -s ".price"

# Interact with existing session (click, fill forms, scroll)
python -m istota.skills.browse interact <id> --click ".button"
python -m istota.skills.browse interact <id> --fill "#email=user@example.com"
python -m istota.skills.browse interact <id> --scroll down --scroll-amount 1000

# Close session
python -m istota.skills.browse close <id>
```

## Output format

```json
{"status": "ok", "title": "...", "url": "...", "text": "...", "links": [{"text": "...", "href": "..."}], "session_id": "..."}
```

The `links` array contains every link on the page. The `session_id` is only present when `--keep-session` is used. Extract returns `{"status": "ok", "selector": "...", "count": N, "elements": [{"text": "...", "html": "...", "href": "...", ...}]}` — elements include key attributes (`href`, `src`, `id`, `class`) from the matched element itself.

## Researching articles from news sites

### Standard workflow (AP News, BBC, Reuters)

Sites where article links appear directly in the `links` array:

1. Fetch the hub/index page with `--keep-session`:
   ```bash
   python -m istota.skills.browse get "https://apnews.com/hub/world-news" --keep-session
   ```
2. Pick articles of interest from the `links` array. Use the `href` values exactly as returned.
3. Navigate to each article using `get --session` with the full URL:
   ```bash
   python -m istota.skills.browse get "https://apnews.com/article/abc123" --session <session_id>
   ```
   This reuses the same browser tab (cookies, referrer, session state preserved). Build the full URL by combining the site origin with the `href` from the links array. The response includes the article's full content.
4. Repeat step 3 for more articles — the session stays alive.
5. Close the session when done:
   ```bash
   python -m istota.skills.browse close <session_id>
   ```

### JS-heavy sites (Guardian, NYT)

Some sites render article links via JavaScript — they won't appear in the standard `links` array. Use `extract` with a CSS selector to find them:

1. Fetch the hub page with `--keep-session`:
   ```bash
   python -m istota.skills.browse get "https://www.theguardian.com/world" --keep-session
   ```
2. If the `links` array has no article links, use `extract` with a site-specific selector:
   ```bash
   python -m istota.skills.browse extract --session <id> -s "a[data-link-name='article']"
   ```
   Each element in the response includes `text`, `href`, and other attributes. Use the `href` to navigate.
3. Navigate to articles using `get --session` as above.

Known selectors: Guardian `a[data-link-name='article']`, CNN `a[data-link-type='article']`. To find others: look at what CSS attributes the site uses for article links. Common patterns: `a[data-link-name]`, `a[data-testid]`, `a[data-link-type]`, `h3 a`, `article a`.

Or use `links --selector` to combine fetch + extract + href parsing in one step:
```bash
python -m istota.skills.browse links "https://www.theguardian.com/world" --selector "a[data-link-name='article']"
```

### Lazy-loaded pages

If the hub page returns few links, scroll to trigger lazy loading:
```bash
python -m istota.skills.browse interact <session_id> --scroll down --scroll-amount 2000
```
**Max 3 scroll rounds** — stop and use what you have.

## Rules

**Run browse commands yourself.** Always execute `python -m istota.skills.browse` directly in Bash. Never delegate browsing to a subtask or subagent — they lose the session context and skill instructions, leading to repeated failures.

**URLs**: Never construct, guess, or modify URLs. Use `href` values from the `links` array. To get the full URL, combine the site's origin with the `href` path (e.g. `https://apnews.com` + `/article/abc123`). If a fetch fails, skip it — do not retry with a guessed variant.

**Failures**: If a site returns an error, empty content, captcha, or no `session_id` — try once more. If it fails twice, skip that site and use an alternative source.

**No debugging**: Never read the browse skill source code, inspect docker containers, curl the browser API directly, test session internals, or debug the browser infrastructure. If the CLI fails, move on.

**Scrolling**: Max 3 rounds. Infinite feeds never end.

## Captcha handling

If a response has `"status": "captcha"`, tell the user and provide the `vnc_url`. Wait for them to solve it, then retry with `--session <session_id>`.

## Notes

- Sessions expire after 10 minutes of inactivity — always close them when done
- Anti-fingerprinting (stealth mode) is enabled by default
- Page text capped at 50,000 chars; links capped at 100 per page
