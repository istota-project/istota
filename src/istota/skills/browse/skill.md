# Web Browsing

You have access to a headless browser for fetching web pages that require JavaScript rendering, have bot detection, or need multi-step interaction.

## When to use

- Pages that return empty/blocked content with curl or httpx (JS-rendered SPAs, Cloudflare-protected sites)
- Sites requiring interaction (clicking, form filling, scrolling)
- Taking screenshots of web pages
- Extracting specific content via CSS selectors
- Multi-step browsing workflows (login → navigate → extract)

For simple static pages or APIs, prefer `curl` or `httpx` directly — they are faster.

## Commands

All commands use subcommands (`get`, `screenshot`, `extract`, `interact`, `close`) with the URL as a positional argument. There is no `--url` flag.

### Browse a page
```bash
python -m istota.skills.browse get "https://example.com"
python -m istota.skills.browse get "https://example.com" --keep-session --timeout 60
python -m istota.skills.browse get "https://example.com" --wait-for "article.content"
```

### Take a screenshot
```bash
python -m istota.skills.browse screenshot "https://example.com" -o /tmp/page.png
python -m istota.skills.browse screenshot --session <id> -o /tmp/page.png --full-page
```

### Extract by CSS selector
```bash
python -m istota.skills.browse extract "https://example.com" -s "article"
python -m istota.skills.browse extract "https://example.com" -s "h1, h2, h3"
python -m istota.skills.browse extract --session <id> -s ".price"
```

### Multi-step interaction
```bash
# Step 1: Open page, keep session
python -m istota.skills.browse get "https://example.com/search" --keep-session

# Step 2: Fill form and click (use session_id from step 1)
python -m istota.skills.browse interact <session_id> --fill "#query=search terms" --click "#submit"

# Step 3: Extract results
python -m istota.skills.browse extract --session <session_id> -s ".results"

# Step 4: Clean up
python -m istota.skills.browse close <session_id>
```

### Interact actions
```bash
python -m istota.skills.browse interact <id> --click ".button"
python -m istota.skills.browse interact <id> --fill "#email=user@example.com" --fill "#name=Alice"
python -m istota.skills.browse interact <id> --scroll down --scroll-amount 1000
```

## Captcha handling

If a page shows a captcha challenge, the response will be:
```json
{
  "status": "captcha",
  "session_id": "abc123",
  "vnc_url": "https://vnc.example.com:6080",
  "message": "Captcha detected. Solve it via VNC, then retry with the same session_id."
}
```

When this happens:
1. Tell the user a captcha was detected and provide the VNC URL
2. Ask them to open the VNC link in their browser and solve the captcha
3. Once they confirm it's solved, retry the original request with `--session <session_id>`

## Output format

Successful browse:
```json
{
  "status": "ok",
  "title": "Page Title",
  "url": "https://example.com",
  "text": "Page text content...",
  "links": [{"text": "Link text", "href": "/path"}],
  "session_id": "abc123"  // only when --keep-session
}
```

Extract:
```json
{
  "status": "ok",
  "selector": "article",
  "count": 2,
  "elements": [{"text": "...", "html": "..."}]
}
```

## Notes

- Sessions expire after 10 minutes of inactivity
- Always close sessions when done with multi-step workflows
- The browser runs with anti-fingerprinting (stealth mode) to reduce bot detection
- Page text is limited to 50,000 characters; links to 100 per page
