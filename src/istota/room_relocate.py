"""Room-token migration inventory; execution is added with the migrator.

These are dispositions, not an instruction to UPDATE every value. Mixed and
structured columns need their named handler. The schema-walking tests keep
both lists complete for named token columns and room foreign keys; embedded
references require a manual audit when their producers change.
"""

# Exact canonical references are rewritten only when they match a migrated
# room. Task and scheduling columns may also contain non-room thread ids.
REWRITE_COLUMNS: dict[tuple[str, str], str] = {
    ("rooms", "token"): "room_token",
    ("rooms", "side_of"): "room_token",
    ("room_bindings", "room_token"): "room_token",
    ("room_members", "room_token"): "room_token",
    ("room_participants", "room_token"): "room_token",
    ("room_read_state", "room_token"): "room_token",
    ("room_dismissals", "room_token"): "room_token",
    ("room_data_grants", "room_token"): "room_token",
    ("room_policy", "room_token"): "room_token",
    ("room_vetoes", "room_token"): "room_token",
    ("room_notices", "room_token"): "room_token",
    ("room_epochs", "room_token"): "room_token",
    ("speech_gate_decisions", "room_token"): "room_token",
    ("messages", "room_token"): "room_token",
    ("message_deletions", "room_token"): "room_token",
    ("web_chat_rooms", "token"): "room_token",
    ("web_chat_messages", "token"): "room_token",
    ("tasks", "conversation_token"): "room_token",
    ("briefing_configs", "conversation_token"): "room_token",
    ("scheduled_jobs", "conversation_token"): "room_token",
    ("channel_sleep_cycle_state", "conversation_token"): "room_token",
    ("credential_grant_rooms", "conversation_token"): "room_token",
    ("outbound_drafts", "room_token"): "room_token",
    ("notifications", "room_token"): "room_token",
    ("user_profiles", "default_room"): "room_token",
    # Parse the descriptor: room:/web: identities differ from talk: refs.
    ("tasks", "output_target"): "descriptor",
    ("scheduled_jobs", "output_target"): "descriptor",
    ("sent_emails", "origin_target"): "descriptor",
    ("outbound_drafts", "origin_target"): "descriptor",
    ("user_profiles", "default_destination"): "descriptor",
    ("user_profiles", "routing"): "routing_json",
    # Multiplayer's email threads store their canonical token here. Historical
    # sent_emails values are Talk refs; private-mail thread hashes stay intact.
    # See transport/email/threads.py::_token_by_stored_mail.
    ("processed_emails", "thread_id"): "email_thread",
    ("sent_emails", "conversation_token"): "email_thread",
    ("memory_chunks", "user_id"): "channel_namespace",
    ("memory_chunks", "source_id"): "channel_path",
    # index_file stores the same path in metadata_json.file_path.
    ("memory_chunks", "metadata_json"): "channel_path_json",
    # Origin: room_token/parent and web channel are canonical, Talk channel is
    # a surface ref. Destination: room_token/parent are canonical; talk_ref,
    # group refs and email thread refs stay intact. Never recursively replace
    # arbitrary strings. Recompute embedded fingerprints with the same helper
    # as their producer, together with the binding_fingerprint columns below.
    ("message_relays", "origin"): "origin_json",
    ("message_relays", "destination"): "destination_json",
    ("whatsapp_skill_requests", "origin"): "origin_json",
    ("whatsapp_skill_requests", "destination"): "destination_json",
    ("message_relays", "binding_fingerprint"): "destination_fingerprint",
    # Includes side_whisper's side_rooms._fingerprint(side, parent), room_post
    # and linked relay destinations; phone binding fingerprints stay intact.
    ("whatsapp_skill_requests", "binding_fingerprint"): "destination_fingerprint",
}

# Surface refs stay native, even where a web ref used to equal rooms.token.
# Credentials and the permanent mapping are included because they too have
# token-bearing column names. Nothing may blanket-rewrite these columns.
PRESERVE_COLUMNS: dict[tuple[str, str], str] = {
    ("room_bindings", "surface_ref"): "native surface reference",
    ("room_participants", "surface_ref"): "native actor identity",
    ("talk_poll_state", "conversation_token"): "Talk poll cursor",
    ("talk_messages", "conversation_token"): "Talk message cache",
    ("tasks", "talk_delivery_token"): "Talk delivery reference",
    ("sent_emails", "talk_delivery_token"): "Talk delivery reference",
    ("sent_emails", "thread_id"): "synthetic mail thread hash",
    ("user_profiles", "log_channel"): "configured Talk reference",
    ("user_profiles", "alerts_channel"): "configured Talk reference",
    # _provisioned_rooms holds Talk refs, not canonical identities. Arbitrary
    # user KV content is not a room-reference schema and must not be rewritten.
    ("istota_kv", "value"): "includes _provisioned_rooms native Talk refs",
    ("room_token_migration", "old_token"): "permanent forwarding source",
    ("room_token_migration", "new_token"): "already minted forwarding target",
    ("google_oauth_tokens", "access_token"): "credential ciphertext",
    ("google_oauth_tokens", "refresh_token"): "credential ciphertext",
    ("google_oauth_tokens", "token_expiry"): "credential expiry timestamp",
    ("web_user_tokens", "access_token"): "credential ciphertext",
    ("web_user_tokens", "refresh_token"): "credential ciphertext",
    ("web_auth_tokens", "token_hash"): "authentication token digest",
    # Approval covers the preview/body, never a re-rendered migration result.
    ("whatsapp_skill_requests", "preview_digest"): "approved content digest",
    ("whatsapp_skill_requests", "approved_digest"): "approved content digest",
    ("whatsapp_skill_requests", "content_hash"): "body digest",
    ("whatsapp_skill_requests", "service_hash"): "body digest",
    ("whatsapp_skill_requests", "template_hash"): "body digest",
}
