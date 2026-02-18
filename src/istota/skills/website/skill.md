# Website Hosting

The user has a static website served at `$WEBSITE_URL`.

## File Access

Files are served directly from `$WEBSITE_PATH` (on the Nextcloud mount under the user's `{BOT_DIR}/html` folder). Create/edit files there to publish content.

- Directory listing (autoindex) is enabled — any directory without an `index.html` shows its contents
- Standard HTML/CSS/JS — no build tools or static site generators needed
- The directory is created automatically if it doesn't exist; use `mkdir -p` as needed

## Examples

```bash
# Create a simple page
echo '<h1>Hello</h1>' > "$WEBSITE_PATH/index.html"

# Create a subdirectory with content
mkdir -p "$WEBSITE_PATH/projects"
cat > "$WEBSITE_PATH/projects/index.html" << 'EOF'
<!DOCTYPE html>
<html><body><h1>Projects</h1></body></html>
EOF
```

## Notes

- Some paths may be restricted to VPN/private network access only
- No server-side processing — static files only
