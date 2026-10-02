/** One coalescing save lane per document. UI and persistence are injected. */
export function createDocumentSaveQueue(row, { put, acknowledged = () => {}, failed = () => {} }) {
  let pending = null; // what the next PUT carries
  let draining = null;
  let conflict = null; // a 409 blocks every write until rebase or discard

  async function drain() {
    while (pending) {
      const saving = pending;
      pending = null;
      try {
        queue.row = await put({ ...saving, expected_revision: queue.row.revision });
        const latest = !pending && queue.draft === saving;
        acknowledged(queue.row, saving, latest);
        if (latest) queue.draft = null;
      } catch (error) {
        pending = queue.draft;
        if (error.status === 409) conflict = error;
        failed(error);
        throw error;
      }
    }
  }

  const queue = {
    row,
    draft: null, // local changes not yet acknowledged
    save(snapshot) {
      queue.draft = { ...queue.draft, ...snapshot };
      pending = queue.draft;
      if (conflict) {
        // Re-offer the unresolved choice; a silent refusal would trap the user in this document.
        failed(conflict);
        return Promise.reject(conflict);
      }
      draining ||= drain().finally(() => {
        draining = null;
      });
      return draining;
    },
    rebase(current) {
      queue.row = current;
      conflict = null;
      pending = null;
    },
    discard(current) {
      queue.rebase(current);
      queue.draft = null;
    },
  };
  return queue;
}
