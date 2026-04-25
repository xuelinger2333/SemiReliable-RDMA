# Debug Protocol — SemiRDMA

**MUST be read at the start of any debugging session for an unexpected experimental result.**

This protocol exists because earlier debug sessions on this project repeatedly:

- Stacked contradictory diagnoses (e.g. "8.5 Gbps per-QP firmware cap" → "1 µs/WR submission cliff", critical rate differing 3×, never reconciled)
- Used motivated reasoning ("Python overhead happens to be 5 µs which happens to be the optimal NIC submission rate" — neither side measured)
- Skipped public-baseline counter-checks (claimed NIC-level behavior without running `ib_write_bw`, the standard tool)
- Proposed paper-level claims from unverified debug findings
- "Fixed" bugs by parameter tuning rather than identifying root cause

The protocol below is mandatory procedural overhead designed to prevent these failure modes.

---

## The Six Rules

### 1. State the hypothesis explicitly

Before any code change, write down:

> **Hypothesis H**: [one specific, testable claim]

Avoid wording like "the receiver might be slow" — too vague to falsify. Acceptable form:

> **Hypothesis H**: Receiver `process_chunk` takes ≥ 5 µs per CQE; sender posting at 1 µs/WR drains SRQ faster than receiver refills, causing silent NIC-level packet drops.

### 2. Derive a falsification experiment before any fix

Write down:

> **If H is true**, experiment X must produce result Y.
>
> **If H is false**, experiment X will produce result Z.

Result Y and Z must be **distinguishable from the same data** — not just "we'll see if it works after the fix". A fix that "happens to work" is not evidence the hypothesis was correct; it is evidence that *some* change in behavior happened.

### 3. Run the falsification experiment FIRST

Do not implement a fix until H has been confirmed by an experiment **designed to falsify** it (one whose alternative outcome would have killed H).

Acceptable: "I will run X expecting Y; if I get Z, H is dead."

Not acceptable: "Let me try fixing X and see if performance improves."

### 4. Reconcile with prior diagnoses

Before adopting a new hypothesis, list **every prior hypothesis** for this bug or related symptoms in the current debugging session, plus relevant ones from `DEBUG_LOG.md`. For each, state:

- Is the new H consistent with that prior diagnosis?
- If not, **explicitly** resolve the contradiction. Either the prior was wrong (say so, with what data killed it) or the new H is wrong, or they describe different sub-mechanisms (specify which observation each one explains).

Stacking contradictory diagnoses without reconciliation is forbidden.

### 5. Distinguish hardware claims from software claims

Before claiming the bug is in NIC / firmware / wire / hardware:

- Run the standard public tool (`ib_write_bw`, `ib_send_bw`, `perftest` family) with **the same configuration** (same `qp_type`, `gid_index`, message size, single QP) and confirm the standard tool exhibits the same anomaly.
- If the standard tool does NOT exhibit the anomaly, the bug is in our software stack, period. Do not propose hardware explanations.

This rule is enforced because:

- Mellanox `ib_write_bw` on CX-5 is publicly documented to hit line rate at single-QP UC.
- If we claim "1 µs submission causes NIC drops" while `ib_write_bw -c UC -q 1` runs at 1.3 µs/WR with 0 loss, our hypothesis is contradicted by trivially-reproducible public data.

### 6. Forbidden moves

These actions are NOT allowed during debugging:

- **No paper contributions from un-falsified findings.** A microbenchmark that is consistent with one explanation but not yet hardened against alternatives must not be proposed as a paper claim. See "STOP and SYNC" below.
- **No "workaround by parameter tuning" without root cause.** If chunk_bytes / sq_depth / timeout_ms changes hide the symptom, identify what changed at the wire / NIC / software level. Paper reviewers will ask.
- **No "the numbers happened to match" as confirmation.** A hypothesis whose only support is one numeric coincidence (e.g. `8.5 Gbps × 14 ms ≈ 33% delivery`) is not confirmed. Test predictions the hypothesis makes about *new* data.
- **No silently re-interpreting prior measurements.** If a new observation forces re-reading old data, write the re-interpretation into `DEBUG_LOG.md` first.

---

## STOP and SYNC

When **any** of the following triggers fire, halt the debugging session and surface the situation to the user before proceeding:

1. **A new diagnosis contradicts an earlier diagnosis in this conversation.** Do not silently retract the earlier one.
2. **A proposed fix changes experimental parameters in ways that affect paper claims** (e.g. switching `chunk_bytes` after results have been collected).
3. **You are about to suggest a paper-level claim or contribution** based on debug findings.
4. **The "fix" amounts to accepting current behavior** rather than fixing it. (User rejected this twice already; default is to dig deeper.)
5. **An experiment result requires re-interpreting previous results.** This is the moment most likely to introduce silent inconsistencies.

When STOP-SYNC triggers, write to the user:

> **STOP-SYNC triggered ([trigger #])**: [one-sentence description of the situation]
>
> Current state: [what's confirmed, what's pending]
>
> What I want to do: [proposed next action]
>
> What you should decide: [the specific question for the user]

Then wait for user input before continuing.

---

## Per-bug logbook

Maintain `DEBUG_LOG.md` (at repo root) with one section per bug investigation. Each section follows the template below. Append to it during the investigation; do not delete prior hypotheses even if they were rejected — the rejection record itself is valuable.

```markdown
## YYYY-MM-DD: <short bug name>

### Symptoms
- <bullet list of observations that opened the investigation>

### Hypothesis A: <claim> [STATUS]
- Predictions if true: <what we'd see>
- Predictions if false: <what we'd see instead>
- Experiment run: <what we did>
- Observation: <what we saw>
- Status: CONFIRMED / REJECTED / PENDING / SUPERSEDED

### Hypothesis B: <claim> [STATUS]
...

### Resolution
<what's the current best explanation, what's still pending,
 what experiments must run before paper claims can be made>
```

`DEBUG_LOG.md` is the source of truth for what's been ruled in / out. CLAUDE.md, code comments, and commit messages must not contradict the log.

---

## Quick checklist (paste this before any debug commit)

```
Before commit:
[ ] Hypothesis H stated as one specific claim
[ ] Falsification experiment described BEFORE the fix
[ ] Experiment was run and yielded the predicted result
[ ] Reconciled with all prior hypotheses for this bug
[ ] If hardware claim: standard tool (ib_write_bw etc) was run with matched config
[ ] No paper contribution proposed without user approval
[ ] DEBUG_LOG.md updated
[ ] No STOP-SYNC trigger fired
```

If any box is unchecked, do not commit.
