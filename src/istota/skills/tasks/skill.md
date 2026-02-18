To create subtasks, write a JSON file to `$ISTOTA_DEFERRED_DIR/task_${ISTOTA_TASK_ID}_subtasks.json`:

```bash
# Create subtasks (processed after this task completes)
cat > "$ISTOTA_DEFERRED_DIR/task_${ISTOTA_TASK_ID}_subtasks.json" << 'EOF'
[
    {"prompt": "Subtask description", "conversation_token": "room42", "priority": 5},
    {"prompt": "Another subtask"}
]
EOF
```

Fields per subtask entry:
- `prompt` (required): The task prompt
- `conversation_token` (optional): Override conversation; defaults to parent's token
- `priority` (optional): 1-10, defaults to 5

The scheduler fills in `user_id`, `source_type`, `parent_task_id`, and `queue` from the parent task.

Environment variables available during execution:
- `ISTOTA_TASK_ID`: Current task ID
- `ISTOTA_USER_ID`: User who requested the task
- `ISTOTA_DEFERRED_DIR`: Directory for deferred operation files
