Nextcloud files are mounted at `/srv/mount/nextcloud/content`. Use standard filesystem operations:

```bash
# List files
ls /srv/mount/nextcloud/content/path/to/folder/

# Read a file
cat /srv/mount/nextcloud/content/path/to/file.txt

# Write to a file
echo "content" > /srv/mount/nextcloud/content/path/to/file.txt

# Create a directory
mkdir -p /srv/mount/nextcloud/content/path/to/newfolder/

# Copy/move files within Nextcloud
cp /srv/mount/nextcloud/content/source.txt /srv/mount/nextcloud/content/dest.txt
mv /srv/mount/nextcloud/content/old.txt /srv/mount/nextcloud/content/new.txt

# Delete a file (use with caution!)
rm /srv/mount/nextcloud/content/path/to/file.txt
```

All changes are saved directly to Nextcloud via the mount. No need to download files to a temp directory first.
