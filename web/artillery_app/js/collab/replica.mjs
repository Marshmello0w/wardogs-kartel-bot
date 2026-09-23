/* Transport-independent optimistic replica. Only acknowledged edits enter undo history. */
// Inject the versioned protocol module; static relative imports would bypass asset versioning.
export function createReplicaClass({ normalizeDocument, applyOperations, coalesceOperations, invertOperations }) {
return class Replica {
    constructor(doc, revision = 0) {
        this.doc = normalizeDocument(doc);
        this.revision = revision;
        this.queue = [];
        this.flight = null;
        this.undoStack = [];
        this.redoStack = [];
    }
    get dirty() { return Boolean(this.flight || this.queue.length); }
    view() {
        let doc = this.doc;
        if (this.flight) doc = applyOperations(doc, this.flight.ops, false);
        return applyOperations(doc, this.queue, false);
    }
    edit(ops) { this.queue = coalesceOperations(this.queue, ops); }
    take(id) {
        if (this.flight || !this.queue.length) return null;
        this.flight = { id, ops: this.queue, mode: 'edit' };
        this.queue = [];
        return { type: 'changes', id, ops: this.flight.ops };
    }
    history(mode, id) {
        if (this.dirty) return null;
        const stack = mode === 'undo' ? this.undoStack : this.redoStack;
        const entry = stack.at(-1);
        if (!entry) return null;
        const ops = mode === 'undo' ? invertOperations(entry) : entry;
        // Fail locally when a newer remote edit made undo unsafe.
        applyOperations(this.doc, ops);
        this.flight = { id, ops, mode, entry };
        return { type: 'changes', id, ops };
    }
    acknowledge(id) {
        if (this.flight?.id !== id) return;
        const { ops, mode, entry } = this.flight;
        if (mode === 'undo') { this.undoStack.pop(); this.redoStack.push(entry); }
        else if (mode === 'redo') { this.redoStack.pop(); this.undoStack.push(entry); }
        else { this.undoStack.push(ops); this.redoStack = []; }
        if (this.undoStack.length > 100) this.undoStack.shift();
        this.flight = null;
    }
    confirm(id, revision) {
        if (this.flight?.id !== id) return;
        if (revision === this.revision + 1) {
            this.doc = applyOperations(this.doc, this.flight.ops);
            this.revision = revision;
        } else if (revision > this.revision) {
            throw new Error('revision-gap');
        }
        this.acknowledge(id);
    }
    reject(id, revision) {
        if (this.flight?.id !== id) return null;
        if (revision !== this.revision) throw new Error('revision-gap');
        const recovery = this.view();
        this.queue = [];
        this.flight = null;
        this.undoStack = [];
        this.redoStack = [];
        return recovery;
    }
    receive(message) {
        if (message.revision <= this.revision) return;
        if (message.revision !== this.revision + 1) throw new Error('revision-gap');
        this.doc = applyOperations(this.doc, message.ops);
        this.revision = message.revision;
        this.acknowledge(message.id);
    }
    snapshot(doc, revision, { ack, rejected } = {}) {
        const recovery = rejected && this.dirty ? this.view() : null;
        this.doc = normalizeDocument(doc);
        this.revision = revision;
        if (ack) this.acknowledge(ack);
        if (rejected) {
            this.queue = [];
            this.flight = null;
            this.undoStack = [];
            this.redoStack = [];
        }
        return recovery;
    }
}
}
