# Adoption note: proto-hive

The proto-hive is Ben Goertzel's hive of long-lived chat bots, and the first hive outside the development team's own to run the record (R1 + R2) for real work. By the release plan's definition of done ("a release is done when at least one hive that is not the development team's own test hive is running it, with a one-page adoption note"), this note is what closes R1 and R2.

**Period:** switched over 2026-10-22 (record initialised 2026-10-20); this note covers up to 2026-10-28. **Log:** about 275 events. **Work:** ten tasks closed after a passed review: a README line, a repository summary asked for by a human, an overview asked for in a private chat, a fix to the bots' own code and its deployment into each of the three Iter bots' live code (with a check that found one bot needed a different patch), and two small code changes. 25 reviews were recorded; 15 of them did not pass (failed, or asked for more evidence) before the work was right. A default branch changed only through a human's merge. One branch reached GitHub without review (below). The first task went from a human's message to a posted result in four minutes.

## Setup

| Choice | proto-hive | Notes |
|---|---|---|
| Profile | `proto-hive` (hive-record 1.4.0, spec amendment A6) | The 1-hive profile without goals: nothing needs a human's approval; the order carries the goal, criteria and a "useless if" line. Leases, supervision, proposals and cost kept. |
| Mode | `authoritative` from the start | Decided with Ben on 2026-10-20: no mirror week. |
| Machines | vm2 (the record, the chief of staff, three bots in one container) and Ben's laptop pop-os (two bots and the reviewer) | pop-os reaches the record over the tailnet. |
| Agents | five long-lived bots (three Iter, two OpenClaw), never restarted for the switch | Iter loads channels each cycle; OpenClaw hot-reloads its config. |
| Front | a chief of staff (Claude Code, own Unix user) | Humans reach it through a Telegram work group. A bot asked for real work in any chat, private ones included, proposes it on the record; the chief of staff approves, rewrites or rejects it, and the reviewed result goes back to the message it came from. |
| Review | Astra (gpt-6-astra through Codex), its own Unix user | Every result, by a different model family from every worker. It now runs under an operator's personal login, until Ben's is available. |
| Repositories | every repository of the `bgoertzel-sing` account (23), plus the workspace | Staged (from 2026-10-28): bots push to a hive copy of each repository on vm2; after a passed review a publisher pushes the branch to GitHub with that repository's deploy key, and a human opens and merges the pull request. `main-watch` reports commits reaching a default branch without a passed review. |

## What it took

- **One adapter per agent kind, outside the agents.** A sidecar per bot holds the key and does the record and git work; the bot sees only a queue (Iter: a channel and a tool; OpenClaw: `hive-task` and a skill). One command puts a bot on or takes it off (`install-agent.py`, `switch-bot.sh`).
- **Chat stays free.** The bots keep talking to humans and each other. Real work asked in chat becomes a proposal on the record; chat is only where a request starts and where its result is shown, never a control signal.
- **Self-modifying agents.** Bots may change their own code; each sidecar re-declares its bot (code commit, uncommitted changes, adapter, WMTM's flags) when it does. Changing a bot's code is a task like any other. The first one, letting the bots cite the chat message a request came from, was done by a bot, failed its first review, passed its second, was merged by a human, and was then deployed by the two bots into their own live code, each as its own task.
- **Staging instead of bot credentials on GitHub.** Bots held keys for one repository at most, so work elsewhere left as a git bundle for a human to push. Now every bot pushes only to hive copies on vm2, and a publisher, holding one deploy key per repository that the owner added once, sends a branch to GitHub only after its review passed. The first staged task went from order to a branch on GitHub in about fifteen minutes.
- **No forced restarts, no new accounts.** The bots' Telegram setup is unchanged; GitHub protection is by rule plus an after-the-fact check rather than new accounts (Ben's choice, to revisit if bots misbehave).

## What worked as intended

- **Review catches real errors.** The repository summary failed review on factual mistakes before it passed; a proposed patch failed because it didn't apply. Most other non-passing reviews asked for evidence the reviewer couldn't reach (a bot's live files on another user's home); the bots answered with readable snapshots and replay harnesses, and the reviewer re-ran them.
- **Self-modification goes through the same loop.** All three Iter bots changed their own live code to cite the chat message a request came from, each as its own reviewed task, with backups, an exact diff, and no restart.
- **The chief of staff edits work, not only routes it.** It rejected a thin proposal (no criteria) and refiled it with a proper order, rejected a duplicate from a second bot, and answered the workers' questions within a minute.

## What broke on the way, and what changed

1. **An unrecorded registry change.** Registering more repositories made the gateway refuse every pin (`PIN_UNAVAILABLE`, `registry_mismatch`) until an operator recorded `hive.registry_changed`. This is the safeguard working, but the step was missed; `up.sh` now warns with the exact command.
2. **Keys under the agents' own user.** On pop-os every agent runs as one user, so keys moved to sidecars under their own users (with ACLs for the shared queue), and the sidecar was made read-only towards the bots' clones.
3. **The reviewer's config was editable by the workers** (it would have lived in the bots' OpenClaw config). Astra now runs its own Codex from a configuration only its user controls.
4. **OpenClaw bots don't poll.** A task never reached them. A Telegram @-mention wake was built and then withdrawn (it puts control signals in the chat tier); the sidecar now delivers straight into the bot as a turn through its gateway's own webhook.
5. **An escalation loop.** With the reviewer unable to run, every resolution of a "no verdict" escalation re-triggered it within two minutes. The supervisor now waits a full window after any escalation or resolution (hive-runtime v0.4.14).
6. **The first live proposals** found two gaps. An OpenClaw bot's tool call waited for an answer that had been delivered as a separate turn (v0.5.14). A proposal the chief of staff rewrote as its own task lost the link back to the asker's message (hive-bridge v0.5.8).
7. **Work on a bot's own code** found three. A second answer to the same question never reached the bot (v0.5.15). The sidecar assumed all work starts from the default branch, but a bot's code lives on a branch with no shared history; operators now set the start branch per bot, never the bot (v0.5.16). The reviewer saw the original order but not the chief of staff's answers that changed it, and failed a correct result (v0.5.17).
8. **The reviewer stopped.** Astra stalled for hours, first on a git race in its workspace (a failed push left a local commit; v0.5.18 resets to the remote before each write), then on its model account's usage limit. Every task waited, and the supervisor's escalations went to the chief of staff, who could only re-explain. hive-bridge v0.5.9 now tells the operators on Telegram when an agent's beacons report a lasting problem.
9. **A bot was silently off the hive for five days.** ProtoCosmo2's sidecar reaches the bot's queue through ACLs on folders in the bot owner's home; a `chmod 700` on one of them cleared the ACL mask, and the sidecar crash-looped from the day after switch-over. Its tasks were escalated and reassigned to the other pop-os bot, so the work still got done, which hid the cause. Fixed 2026-10-28; recorded in `LIMITS.md` with how to spot it.
10. **Deployment papercuts:** Python installed under root's home, a token file rewritten as root, long lines broken when pasted into terminals, an SSH port open to the internet that drops operator connections. Each is handled by the scripts or listed in `LIMITS.md`.

## Where the proto-hive falls short

- It does **not** conform to SPEC §6.6 (key custody): the agents share an OS user on pop-os and a container on vm2. Keys are held apart, but an event signed by a pop-os bot proves only that some process on that machine asked its sidecar. The deviation is declared in each actor's declaration.
- **Staging holds only where the bots have no GitHub credentials of their own.** On 2026-10-28 the first staged task from pop-os was pushed by the bot straight to GitHub, as the account owner, with credentials its OS user can reach; the hive copy got nothing and the reviewer found the work on GitHub. Nothing was merged. The same day the bots' OS user lost that login (the owner's own login stays), so on pop-os the rule no longer rests on the bots' instructions alone; one vm2 bot can still reach the owner's raw credentials.
- **Publishing depends on one reviewer.** When Astra is down, reviewed work stops short of GitHub (results wait in the hive copies) and every task waits.
- **The chief of staff can reach a worker only through a blocked task.** After a failed review, what it knows waits until the worker asks.

Each is listed, with what would fix it, in the deployment's `LIMITS.md`.

## Open at the time of writing

- Astra on Ben's own login.
- Keeping the raw GitHub token one bot can reach out of its reach (a GitHub App per bot, its key held by the sidecar, is one way).
- Separate OS users for the pop-os bots (§6.6).
