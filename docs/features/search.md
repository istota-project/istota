# Search

Open Search beside the notification bell, or press Cmd+K on macOS or Ctrl+K elsewhere. `/` also opens it when you are outside a text field. Enter at least two characters to search; results update as you type.

Results are grouped by source. Use the up and down arrows to select a result, Enter to open it, and Esc to close Search. Choose a source in the dropdown beside the search field to see more results from that source, then **Show more** to continue. Recent queries appear when the input is empty. A query is saved in this browser only when you open a result, and Search keeps the eight most recent.

## What you can find

| Source | Content | Opens |
| --- | --- | --- |
| Chats | Messages in rooms you can currently read | The matching message in its room |
| Rooms | Room names | The room |
| Memory | Indexed memory files, USER.md, channel notes and playbooks | A file preview or the room |
| Facts | Current knowledge facts | The fact appears in Search |
| Briefings | Archived subjects and bodies | The briefing |
| Feeds | Entry titles, authors and article text | The entry reader |
| Health | Your documents, lab panels and markers, encounters, diagnoses and immunizations | The matching health record |
| Location | Saved place names, categories and notes | The place on the map |
| Transactions | Payees and narrations in your default ledger | Transactions for that account and year |

Module sources appear only when that module is available and enabled for you. Transactions run only after you choose **Search Transactions** or select Transactions in the source dropdown, because querying a ledger takes longer than the other sources. This uses the transactions page's filter: regular expressions and `#tag` queries work, and words are joined into one filter. There is no partial-match fallback for transactions. Results are posting rows, so one transaction split across accounts can appear more than once.

Search uses your current room membership. Shared-room messages include other participants' turns that you can already read. Archived rooms, dismissed rooms and hidden email threads are excluded. Channel memory includes notes filed under a room's earlier identifiers. Health reference material, such as general biomarker explanations, is not searched.

## Matching

Search normally requires every word. A source with no exact matches can return partial matches and says so below its label. Sources keep their own ordering; results are not ranked against unrelated sources.

Chats and feeds match word prefixes and quoted phrases. Memory uses its existing text index; quoted memory phrases are searched as separate words. Other sources use substring matching over their text fields. Search is text-based and does not use vector embeddings or change the chat `!search` command.

Chats, feeds and indexed memory ignore common accent differences, so `cafe` can match `café`. Case folding for the smaller SQLite sources is ASCII-only: `ÉTÉ` need not match `été`. A continuous run of CJK characters is one token in the full-text indexes; a substring of that run will not match. Try a complete word or a different source when these limits affect a query.

## Opening results

A chat result can load older history in one request and mark the message in the transcript. One jump can load at most 2,000 durable rows, including system notes. A target beyond that range leaves the room selected and reports that it is too far back to open. You can still read its snippet in Search. A message deleted since the search may no longer be locatable.

Memory files open only when the file viewer can serve the path for your account. An unavailable file remains a readable search result without an action. Search does not scan arbitrary workspace files or search their filenames.

A source that times out or fails shows **Couldn't search** beside its name; the other sources still return their results. A network failure leaves the query in the dialog so that the next edit can retry.

## Operation

There are no new settings. Search reads each source's existing store. Transcript and feed indexes are updated with inserts, edits and deletions. The first database upgrade rebuilds those two indexes from existing rows; other sources do not copy their content into a central index.

The API is `GET /istota/api/search`. It uses the signed-in user's identity and accepts `q`, comma-separated `sources`, `limit` and `offset`. Queries are capped at 200 characters and eight parsed terms. The default view returns up to five results per source; a selected source returns twenty in the dialog. API limits are 1–25 results and an offset of 0–500.
