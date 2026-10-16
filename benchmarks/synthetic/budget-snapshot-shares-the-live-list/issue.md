A ledger snapshot changes when the ledger keeps growing.

`snapshot` exists so a background job can hold onto the state it read. Callers that sort or trim their copy are seeing those edits in the ledger itself, and the exported totals then disagree with the live ones.
