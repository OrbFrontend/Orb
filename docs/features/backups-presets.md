# Backups and Presets

Orb stores characters, conversations, Worlds, fragments, the phrase bank,
settings, and endpoints in one SQLite database. **Backup & Presets** saves this
data as portable `.db` files.

Open it from **Settings → 💾 Backup & Presets**.

## Backups and presets

Both are database snapshots stored in the same library:

- A **backup** contains all domains and can restore the database to that state.
- A **preset** contains selected domains and is useful for sharing or merging.

Library entries have one of these labels:

| Label | Meaning |
|---|---|
| `manual` | Created with **Snapshot current**. |
| `auto` | Created before a destructive operation. Orb keeps the 10 newest. |
| `imported` | Added with **Import file**. |

## Create a snapshot

Select **Snapshot current**, choose the domains, add an optional label, and select
**Create**.

| Domain | Included data |
|---|---|
| Characters | Character cards |
| Chats | Conversations and message branches |
| Lorebooks | Worlds and entries |
| Fragments | Mood and Director fragments |
| Phrase bank | Phrase-bank entries |
| Settings & endpoints | Settings, endpoints, model configuration, and personas |

Selecting **Chats** also includes **Characters**, because a conversation needs its
character.

!!! warning "API keys"
    **Settings & endpoints** includes endpoint configuration and API keys. Keep
    **Strip API keys** enabled when you share a file. If you do not include this
    domain, Orb removes personal configuration and secrets from the snapshot.

## Apply or restore

Use **Apply** to merge a file into the current database. Matching items are
overwritten and new items are added. Other items stay unchanged.

Use **Restore** to replace the domains covered by the file. A full backup replaces
the whole database. A partial file replaces only its included domains; everything
else stays unchanged.

!!! danger "Restore removes data"
    Restore removes items added after the snapshot in the domains it covers. Use
    Apply when you want to combine data.

## Import and safety backups

**Import file** adds an external `.db` to the library without changing live data.
You must later choose **Apply** or **Restore**. Orb validates the file and updates
older Orb snapshots to the current schema. Files from a newer Orb version are
rejected until Orb is updated.

Orb creates an automatic backup before **Apply** and **Restore**. If the result is
wrong, restore the newest `auto` entry. Orb keeps the 10 newest automatic backups.
