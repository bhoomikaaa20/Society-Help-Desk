# Society Desk: Project Guide

Society Desk lets apartment residents report maintenance problems in Discord. Three AI agents log the problem, find the right vendor and request the work order. A manager approves the money, and the resident confirms the repair.

All data in this project is fictional.

## How it fits together

```text
Resident or manager
        |
     Discord          where people chat
        |
     Switch           puts the agents in Discord and asks for approvals
        |
  Claude Code agents  helpdesk -> operations -> treasurer
        |
  Python and SQLite   stores the data and enforces the rules
```

| Agent | Job |
|---|---|
| `helpdesk` | Talks to residents, creates tickets and closes them after the resident confirms |
| `operations` | Checks past failures, vendors and budget, then recommends a work order |
| `treasurer` | Requests the work order, which waits for the manager's answer |

Two people have to agree before a job is finished:

- **The manager** approves the money, before the repair.
- **The resident** confirms the problem is fixed, after the repair.

## What you need

- Python 3
- Claude Code, installed and signed in
- Docker and [Switch Console](https://docs.switchagents.ai/)
- A Discord server where you can add a bot

All three agents must run on the same computer, because they share one database file.

## 1. Set up the project

```powershell
git clone https://github.com/bhoomikaaa20/Society-Help-Desk.git
cd Society-Help-Desk
python tools/desk.py init
python -m unittest discover -s tests
```

All tests should pass. There are no Python packages to install.

## 2. Connect Discord to Switch

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application and copy the bot token from the **Bot** tab.
2. On the same tab, turn on **Server Members Intent** and **Message Content Intent**.
3. Under **OAuth2 > URL Generator**, choose the scopes `bot` and `applications.commands`, and these permissions: View Channels, Send Messages, Send Messages in Threads, Manage Webhooks, Manage Channels, Manage Roles, Read Message History, Attach Files and Add Reactions. Open the generated URL to add the bot to your server.
4. Turn on Developer Mode in Discord, then right-click your server and copy its ID.
5. In Switch Console, go to **Home > Messaging apps > Connect > Discord**. Enter the bot token and the server ID (**Guild Id**), then link your Discord account.

## 3. Create the agents

In Switch Console, under **Your Agents**, create three Claude Code agents. Set **Directory** to the folder you cloned, and check it carefully, because it may not be changeable later.

| Agent name | Instructions to paste | Who can talk to it | Auto mode | Bypass permissions |
|---|---|---|---|---|
| `helpdesk` | `switch/helpdesk.md` | Residents and your agents | On | **Off** |
| `operations` | `switch/operations.md` | Me and my agents | On | **Off** |
| `treasurer` | `switch/treasurer.md` | Me and my agents | **Off** | **Off** |

`CLAUDE.md` in the project folder sets how the agents write. Claude Code should read it automatically.

> **Important:** Treasurer's Auto mode must be off. With it on, we saw a work order created and money spent without anyone approving it.

## 4. Add the agents to a channel

Create a Discord channel and post `hello`. The bot links the channel to Switch. Then send these one at a time:

```text
!invite-agent @helpdesk
!invite-agent @operations
!invite-agent @treasurer
```

## 5. Test the full flow

Start with clean data:

```powershell
python tools/desk.py init --reset --yes
```

This deletes all tickets and work orders created since the import.

Then send each message in the channel, starting it with the agent's `@name`:

| Step | Send | You should see |
|---|---|---|
| 1 | `@helpdesk The lift in Block B stopped working.` | "Is anyone trapped in the lift?" |
| 2 | `@helpdesk No.` | Ticket `T-031`, then a summary from Operations: 3 past failures, EUR 8,170, vendor `V-04`, about EUR 3,200 |
| 3 | Answer the permission request with **Decline** or `no` | "Declined. No money was spent." Treasurer asks why, and what should happen next |
| 4 | `@treasurer The quote is too high. Next step: get a cheaper quote.` | Helpdesk gives the resident a kind update with no money details, and asks Operations for another option |
| 5 | Answer the new permission request with **Allow once** or `yes` | `WO-001` created. Budget left depends on the vendor Operations picked |
| 6 | `@helpdesk What's happening with T-031?` | Its progress and estimated repair time |
| 7 | `@helpdesk The vendor finished T-031. The lift controller was replaced.` | "Has the maintenance issue been resolved successfully?" |
| 8 | `@helpdesk Yes, it's working now.` | "Great! What is your flat number?" |
| 9 | `@helpdesk B-204` | "Thank you! Ticket T-031 is now closed." |

To check the results yourself:

```powershell
python tools/desk.py history T-031
python tools/desk.py budget
```

## If something goes wrong

| Problem | What to do |
|---|---|
| A work order is created with no request to answer | Stop. Turn off Treasurer's Auto mode, then reset the data and test again. |
| You can't find Allow or Decline | Look for a `Permission needed` message, including in collapsed `1 Message` previews and in Switch Console. Answer with `yes` or `no`, or the format the message shows. |
| An agent writes long messages or asks for a flat number first | Check that `CLAUDE.md` is in the agent's Directory, then try a new channel. |
| An agent doesn't reply | Start the message with its `@name`, and check that it's in the channel. |
| An agent can't find `python` | Use the full path to `python.exe` in its instructions. |
| Ticket numbers don't match this guide | Reset the data. |

## Project files

| Path | What it is |
|---|---|
| `tools/` | The backend. `desk.py` is the command the agents run. |
| `data/` | Starting vendors, budgets and past tickets |
| `database/` | The local database, created by `init` and not stored in git |
| `switch/` | Each agent's job instructions |
| `CLAUDE.md` | How the agents write messages |
| `tests/` | Automatic tests |
| `README.md` | Full reference: all commands, backend rules and limitations |

## Rules that matter

- Don't run `work-order` by hand during a demo. It skips the manager's approval, because Python can't tell whether a person approved.
- Always choose **Allow once**. **Allow for this session** may let later work orders run without asking.
- Use only fictional data.
