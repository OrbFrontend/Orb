/** One coalescing save lane per document. UI and persistence are injected. */
export function createDocumentSaveQueue(row, { put, acknowledged = () => {}, failed = () => {} }) {
  let pending = null;
  let draining = null;
  let conflict = null;
  const queue = {
    row,
    draft: null,
    get conflict() {
      return conflict;
    },
    get saving() {
      return draining != null;
    },
    async save(snapshot) {
      queue.draft = { ...(queue.draft || {}), ...snapshot };
      pending = queue.draft;
      if (conflict) throw conflict;
      if (!draining) {
        draining = Promise.resolve()
          .then(async () => {
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
          })
          .finally(() => {
            draining = null;
          });
      }
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
