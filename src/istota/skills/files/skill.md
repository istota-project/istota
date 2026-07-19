---
name: files
description: File operations in your workspace
always_include: true
env: [{"var":"NC_URL","from":"config","config_path":"nextcloud.url"},{"var":"NC_USER","from":"config","config_path":"nextcloud.username"},{"var":"NC_PASS","from":"config","config_path":"nextcloud.app_password","sensitive":true}]
---
Your workspace files live under `{workspace}`. Use standard filesystem operations:

```bash
# List files
ls {workspace}/path/to/folder/

# Read a file
cat {workspace}/path/to/file.txt

# Write to a file
echo "content" > {workspace}/path/to/file.txt

# Create a directory
mkdir -p {workspace}/path/to/newfolder/

# Copy/move files within your workspace
cp {workspace}/source.txt {workspace}/dest.txt
mv {workspace}/old.txt {workspace}/new.txt

# Delete a file (use with caution!)
rm {workspace}/path/to/file.txt
```

Changes are saved directly to your workspace. No need to download files to a temp directory first.

**Attachment troubleshooting:** If a shared file isn't at the expected path, it may not have reached your workspace inbox yet. On a Nextcloud-backed deployment this usually means the user hasn't shared their Talk attachments folder (e.g. `Talk/` or `Talk (2)/`) with the bot user, or there's a short delay (~2 minutes) after sharing before files become accessible — let them know so they can share the folder.
