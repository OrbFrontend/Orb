# Backups & Presets

Orb keeps all of your data — characters, chats, lorebooks, fragments, phrase bank, settings — in a single SQLite database. **Backups & Presets** lets you capture that data into portable `.db` files, share subsets of it, and roll your live database back to an earlier state.

Open it from **Settings → 💾 Backup & Presets**.

## Backups vs. presets

Both are the same kind of file — a standalone `.db` snapshot — and live together in one **library**. The difference is only in *what they carry*:

- A **backup** covers **everything**. Because it holds your whole database, it can be *restored* to roll the app back to exactly that state.
- A **preset** covers a **subset** (say, just your characters and lorebooks). It's portable — meant to be downloaded and shared, then merged into someone else's data.

The library lists every file uniformly with a coloured tag for its **kind**:

| Kind | Where it comes from |
|---|---|
| `manual` | You created it with **Snapshot current**. |
| `auto` | Orb took it automatically before a destructive action (see [Safety net](#safety-net)). Auto backups are pruned to the most recent 10. |
| `imported` | You brought it in from outside with **Import file**. |

## Creating a snapshot

Click **📸 Snapshot current** and pick what to include:

| Domain | Contents |
|---|---|
| Characters | Character cards |
| Chats | Conversations and their full message trees (*requires* Characters) |
| Lorebooks | Worlds and lorebook entries |
| Fragments | Mood & director fragments |
| Phrase bank | Phrase bank entries |
| Settings & endpoints | App settings, endpoints, model configs, personas |

Check **everything** to make a full backup you can restore from. Check a subset to make a portable preset.

!!! note "Chats need their characters"
    A conversation is meaningless without the character it's about, so selecting **Chats** automatically pulls in **Characters**.

!!! warning "API keys"
    If you include **Settings & endpoints**, the snapshot contains your endpoint configuration — including API keys. A **Strip API keys** option appears (on by default); leave it checked when sharing a preset so your keys never leave your machine. If you *don't* include Settings & endpoints, all personal config and secrets are scrubbed from the file automatically.

Add an optional **Label** to make the file easy to recognise later, then click **Create**.

## Bringing data in

Each entry in the library offers two ways to bring its data into your live database — plus **Download** (save the `.db` to disk, e.g. to share it) and **Delete**.

### Apply — merge

**Apply** merges the file's data into what you already have, **by identity**:

- Matching items are **overwritten** with the file's version; items the file doesn't mention are **left alone**.
- New items are **added**.

This is the right choice for installing a shared preset — you gain its characters or lorebooks without losing your own.

### Restore — roll back

**Restore** rolls your data back to match the file. How much it touches depends on the file's coverage:

- A **full backup** (all domains) is swapped in whole — a clean, total rollback of every domain.
- A **partial file** is restored **domain-scoped**: each domain the file carries is replaced to match the file *exactly* (anything you've added to those domains since is removed), while domains the file doesn't carry are left untouched.

!!! danger "Restore is destructive"
    Unlike Apply, Restore *removes* data within the domains it covers. Use it to undo changes or recover a known-good state, not to combine two sets of data.

## Importing an external file

Click **⬆ Import file…** and choose a `.db` file. Importing is **non-destructive** — it only adds the file to your library. Nothing changes in your live data until you then choose **Apply** or **Restore** on it.

On import, Orb validates that the file is a genuine Orb database and migrates it up to your current schema if it was made by an older build.

!!! note "Version skew"
    A file produced by a *newer* version of Orb is rejected — update Orb before importing it.

## Safety net

Every destructive action — **Apply**, **Restore**, and importing-then-applying — is preceded by an **automatic backup** of your current state. If a result isn't what you wanted, that `auto` entry sits at the top of your library, ready to **Restore**. Orb keeps the 10 most recent auto backups and prunes older ones.
