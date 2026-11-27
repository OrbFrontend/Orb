# Backups

Orb stores characters, conversations, Worlds, profiles, settings, and endpoints
in one SQLite database. **Backups** saves this data as portable `.db` files.

Open it from **Settings → 💾 Backups**.

## Full and partial backups

Every snapshot is stored in the same library:

- A **full backup** contains all domains and every part of every profile, and can
  restore the database to that state.
- A **partial backup** contains what you selected and is useful for sharing or merging.

Library entries have one of these labels:

| Label | Meaning |
|---|---|
| `manual` | Created with **Snapshot current**. |
| `auto` | Created before a destructive operation. Orb keeps the 10 newest. |
| `imported` | Added with **Import file**. |

## Create a snapshot

Select **Snapshot current**, choose the domains, then what to save of each
[profile](profiles.md), add an optional label, and select **Create**.

| Domain | Included data |
|---|---|
| Characters | Character cards |
| Chats | Conversations and message branches |
| Lorebooks | Worlds and entries |
| Documents | Documents |
| Settings & endpoints | Settings, endpoints, model configuration, and personas |

| Profile part | Included data |
|---|---|
| Setup | The switches, lane models, and Prompt Layout |
| Fragments | Mood and Director fragments |
| Phrase bank | Phrase-bank entries and dismissed suggestions |
| Global lorebooks | Which Worlds are global |

Selecting **Chats** also includes **Characters**, because a conversation needs its
character. Selecting **Global lorebooks** also includes **Lorebooks**, because a
mark needs its World.

!!! warning "API keys"
    **Settings & endpoints** includes endpoint configuration and API keys. Keep
    **Strip API keys** enabled when you share a file. If you do not include this
    domain, Orb removes personal configuration and secrets from the snapshot.

## Apply or restore

Use **Apply** to merge a file into the current database. Matching items are
overwritten and new items are added. Other items stay unchanged.

Use **Restore** to replace the domains covered by the file. A full backup replaces
the whole database. A partial file replaces only its included domains and profile
parts; everything else stays unchanged.

Apply and Restore list each profile in the file with its parts. Clear a part to
leave it out, and choose where the profile goes: onto the profile it came from,
when this database has it, or onto a **New profile**. A profile's parts replace
only that profile's parts; other profiles are untouched. The active profile stays
active, and its switches and lane models stay as they were unless the file
restores its setup.

A **New profile** gets the built-in fragments and phrase bank for the parts it
did not take, and the built-in Prompt Layout without **Setup**, as **New…** in
the picker does.

A setup saved without **Settings & endpoints** names each lane's endpoint by its
URL and its model by name. Switching to the profile picks the endpoint here with
that URL and its model configuration with that name.

!!! danger "Restore removes data"
    Restore removes items added after the snapshot in the domains it covers. Use
    Apply when you want to combine data.

## Import and safety backups

**Import file** adds an external `.db` to the library without changing live data.
You must later choose **Apply** or **Restore**. Orb validates the file and updates
older Orb snapshots to the current schema. Files from a newer Orb version are
rejected until Orb is updated.

Orb creates an automatic backup before **Apply** and **Restore**. If the result is
wrong, restore the newest `auto` entry. For a partial backup, that safety backup may
cover only the parts the file can change. Orb keeps the 10 newest automatic backups.
