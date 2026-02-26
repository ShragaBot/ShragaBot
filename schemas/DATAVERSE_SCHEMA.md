# Dataverse Table Schema Documentation

Complete reference for recreating all Shraga Dataverse tables in a new environment.
An agent following these instructions should be able to reproduce the entire schema from scratch.

---

## Table of Contents

1. [Environment Prerequisites](#1-environment-prerequisites)
2. [Solution Publisher Prefixes](#2-solution-publisher-prefixes)
3. [Table Creation Order](#3-table-creation-order)
4. [Table: cr_shraga_conversations](#4-table-cr_shraga_conversations)
5. [Table: cr_shraga_tasks](#5-table-cr_shraga_tasks)
6. [Table: cr_shragamessages](#6-table-cr_shragamessages)
7. [Table: crb3b_shragausers](#7-table-crb3b_shragausers)
8. [Picklist / Choice Definitions](#8-picklist--choice-definitions)
9. [Row-Level Security Configuration](#9-row-level-security-configuration)
10. [Step-by-Step Creation Instructions](#10-step-by-step-creation-instructions)
11. [Validation Checklist](#11-validation-checklist)

---

## 1. Environment Prerequisites

- A Dataverse environment with maker/admin access
- Power Apps maker portal access (https://make.powerapps.com)
- Two solution publishers configured (see section 2)
- Azure AD app registration or user credentials for API access
- The Dataverse environment URL (e.g., `https://orgXXXXXXXX.crm3.dynamics.com`)

---

## 2. Solution Publisher Prefixes

Two solution publisher prefixes coexist across the tables. This is a historical artifact -- the original Shraga v1 used one publisher, and later additions used a different one. Both must be created.

| Publisher Prefix | Origin | Used In |
|---|---|---|
| `cr_` | Original Shraga v1 solution publisher | cr_shraga_conversations, cr_shraga_tasks, cr_shragamessages (base columns) |
| `crb3b_` | Later solution publisher | crb3b_shragausers (entire table), plus additional columns on cr_shraga_tasks and cr_shragamessages |

**Important:** When creating tables and columns in Power Apps, the prefix is determined by which solution you are working in. You must:
1. Create a solution with the `cr` publisher prefix (customization prefix = `cr`)
2. Create a solution with the `crb3b` publisher prefix (customization prefix = `crb3b`)
3. Add tables/columns to the appropriate solution based on their prefix

---

## 3. Table Creation Order

Tables must be created in this order due to conceptual dependencies (no formal Dataverse lookups between them, but the system expects all tables to exist):

1. **cr_shraga_conversations** -- no dependencies
2. **cr_shraga_tasks** -- no dependencies
3. **cr_shragamessages** -- references task IDs by plain string (not a formal lookup)
4. **crb3b_shragausers** -- no dependencies

---

## 4. Table: cr_shraga_conversations

**Purpose:** Message bus between the Copilot Studio bot and task managers. Each row represents one message -- either inbound (user to manager) or outbound (manager to user).

| Property | Value |
|---|---|
| Logical Name (entity) | `cr_shraga_conversation` |
| Logical Name (collection/plural) | `cr_shraga_conversations` |
| Display Name | Shraga Conversations |
| Solution Publisher Prefix | `cr_` |
| Primary Key Column | `cr_shraga_conversationid` (GUID, auto-generated) |
| Primary Name Column | `cr_name` |
| Ownership Type | User or Team |

### Columns

| # | Logical Name | Schema Name | Display Name | Data Type | Max Length | Required | Description |
|---|---|---|---|---|---|---|---|
| 1 | `cr_shraga_conversationid` | `cr_shraga_conversationid` | Shraga Conversation | Uniqueidentifier (GUID) | -- | Auto | Primary key, auto-generated |
| 2 | `cr_name` | `cr_name` | Name | Single Line of Text (String) | 100 | Required (Business Required) | Display name / first line of message, auto-truncated to 100 chars |
| 3 | `cr_useremail` | `cr_useremail` | User Email | Single Line of Text (String) | 200 | Optional | User email address (from System.User.Email in bot) |
| 4 | `cr_mcs_conversation_id` | `cr_mcs_conversation_id` | MCS Conversation ID | Single Line of Text (String) | 500 | Optional | Bot conversation ID (from System.Conversation.Id). Used to route responses back and for isFollowup filter |
| 5 | `cr_message` | `cr_message` | Message | Multiple Lines of Text (Memo) | 100000 | Optional | Full message text (multiline) |
| 6 | `cr_direction` | `cr_direction` | Direction | Single Line of Text (String) | 50 | Optional | Message direction. Values: `Inbound` or `Outbound` |
| 7 | `cr_status` | `cr_status` | Status | Single Line of Text (String) | 50 | Optional | Processing status. Values: `Unclaimed`, `Claimed`, `Processed`, `Delivered` |
| 8 | `cr_claimed_by` | `cr_claimed_by` | Claimed By | Single Line of Text (String) | 200 | Optional | Agent instance ID that claimed this message (e.g., `ps:sagik@microsoft.com` or `gs`) |
| 9 | `cr_in_reply_to` | `cr_in_reply_to` | In Reply To | Single Line of Text (String) | 100 | Optional | GUID of the inbound row this outbound message responds to. Used by the SendMessage flow to match responses |
| 10 | `cr_followup_expected` | `cr_followup_expected` | Follow-up Expected | Single Line of Text (String) | 10 | Optional | Whether the bot should expect a follow-up message. Values: `"true"` or `""` (empty string). **MUST be String type, NOT Boolean** |
| -- | `createdon` | `createdon` | Created On | DateTime | -- | Auto | Auto-set by Dataverse |
| -- | `modifiedon` | `modifiedon` | Modified On | DateTime | -- | Auto | Auto-set by Dataverse |

### String Values for cr_direction

| Value | Meaning |
|---|---|
| `Inbound` | User sent a message to the bot (user -> manager) |
| `Outbound` | Manager is sending a response to the user (manager -> user) |

### String Values for cr_status

| Value | Meaning |
|---|---|
| `Unclaimed` | New row, not yet picked up by any manager |
| `Claimed` | A manager has claimed this message (via ETag atomic concurrency) |
| `Processed` | The inbound message has been fully processed |
| `Delivered` | The outbound response has been read by the SendMessage flow |
| `Expired` | The outbound response was never delivered and has been marked stale by cleanup |

### Relationships

None (no formal Dataverse lookup relationships). References to other tables are by plain string ID values.

### Key Usage Patterns

- **Inbound row creation:** SendMessage flow writes row with `direction=Inbound`, `status=Unclaimed`
- **Claiming:** Manager PATCHes `status=Claimed` + `claimed_by={manager_id}` using If-Match ETag header (HTTP 412 = lost race)
- **Response:** Manager POSTs new outbound row with `direction=Outbound`, `status=Unclaimed`, `in_reply_to={inbound_row_id}`
- **Delivery:** SendMessage flow reads outbound row, PATCHes `status=Delivered`
- **Cleanup:** Stale unclaimed outbound rows are periodically marked as `Expired` to prevent filter contamination

---

## 5. Table: cr_shraga_tasks

**Purpose:** Tracks AI coding tasks through their full lifecycle (Pending -> Queued -> Running -> Completed/Failed/Canceled).

| Property | Value |
|---|---|
| Logical Name (entity) | `cr_shraga_task` |
| Logical Name (collection/plural) | `cr_shraga_tasks` |
| Display Name | Shraga Tasks |
| Solution Publisher Prefix | `cr_` (base table), `crb3b_` (additional columns) |
| Primary Key Column | `cr_shraga_taskid` (GUID, auto-generated) |
| Primary Name Column | `cr_name` |
| Ownership Type | User or Team |

### Columns

| # | Logical Name | Schema Name | Display Name | Data Type | Max Length | Required | Description |
|---|---|---|---|---|---|---|---|
| 1 | `cr_shraga_taskid` | `cr_shraga_taskid` | Shraga Task | Uniqueidentifier (GUID) | -- | Auto | Primary key, auto-generated |
| 2 | `cr_name` | `cr_name` | Task ID | Single Line of Text (String) | 100 | Required (Business Required) | Task title / display name. Populated with first 100 chars of prompt |
| 3 | `cr_prompt` | `cr_prompt` | Prompt | Multiple Lines of Text (Memo) | 100000 | Optional | Full task description from user |
| 4 | `cr_status` | `cr_status` | Status | **Picklist (Whole Number)** | -- | Optional | Task lifecycle status. See Picklist values below |
| 5 | `cr_statusmessage` | `cr_statusmessage` | Status Message | Single Line of Text (String) | 200 | Optional | Human-readable status detail (e.g., "Claimed by CPC-sagik-AAG29") |
| 6 | `cr_result` | `cr_result` | Result | Multiple Lines of Text (Memo) | 100000 | Optional | Task result (final output, markdown formatted with bullet points and OneDrive links) |
| 7 | `cr_transcript` | `cr_transcript` | Transcript | Multiple Lines of Text (Memo) | 1048576 | Optional | Full execution transcript (JSONL format) |
| 8 | `cr_userid` | `cr_userid` | User ID | Single Line of Text (String) | 200 | Optional | Dataverse system user GUID or email. Legacy column used for task filtering |
| 9 | `crb3b_useremail` | `crb3b_UserEmail` | User Email | Single Line of Text (String) | 200 | Optional | User email address (preferred identifier for task ownership) |
| 10 | `crb3b_devbox` | `crb3b_devbox` | Dev Box | Single Line of Text (String) | 200 | Optional | Machine hostname (e.g., `CPC-sagik-AAG29`). Used for per-devbox task scheduling |
| 11 | `crb3b_workingdir` | `crb3b_workingdir` | Working Directory | Single Line of Text (String) | 500 | Optional | OneDrive session subfolder path on dev box (e.g., `C:\Users\sagik\OneDrive\Shraga Sessions\fix-login-css_a1b2c3d4`) |
| 12 | `crb3b_deeplink` | `crb3b_deeplink` | Deep Link | Single Line of Text (String) | 1000 | Optional | Teams deep link URL to the Running card in Workflows chat |
| 13 | `crb3b_runningchatid` | `crb3b_RunningChatID` | Running Chat ID | Single Line of Text (String) | 500 | Optional | Workflows chat ID (`19:xxx@thread.v2`). Written by TaskRunner flow |
| 14 | `crb3b_runningmessageid` | `crb3b_RunningMessageID` | Running Message ID | Single Line of Text (String) | 500 | Optional | Running card message ID in Workflows chat. Written by TaskRunner flow |
| 15 | `crb3b_onedriveurl` | `crb3b_onedriveurl` | OneDrive URL | Single Line of Text (String) | 1000 | Optional | OneDrive web URL for the session folder. Written at task start so even failed tasks have the link |
| 16 | `crb3b_sessionsummary` | `crb3b_sessionsummary` | Session Summary | Multiple Lines of Text (Memo) | 1048576 | Optional | Structured JSON session summary (cost, duration, tokens, session ID, dev box, activities, phases, model usage). Written on every terminal state |
| 17 | `crb3b_chatid` | `crb3b_ChatID` | Chat ID | Single Line of Text (String) | 500 | Optional | Legacy/supplementary chat ID field |
| 18 | `crb3b_conversationid` | `crb3b_ConversationID` | Conversation ID | Single Line of Text (String) | 500 | Optional | Legacy/supplementary conversation ID field |
| 19 | `crb3b_messageid` | `crb3b_MessageID` | Message ID | Single Line of Text (String) | 500 | Optional | Legacy/supplementary message ID field |
| 20 | `crb3b_controlchatid` | `crb3b_ControlChatId` | Control Chat ID | Single Line of Text (String) | 500 | Optional | Legacy/supplementary control chat ID |
| 21 | `crb3b_controlmessageid` | `crb3b_ControlMessageId` | Control Message ID | Single Line of Text (String) | 500 | Optional | Legacy/supplementary control message ID |
| 22 | `crb3b_lastcardupdatetime` | `crb3b_LastCardUpdateTime` | Last Card Update Time | DateTime | -- | Optional | Timestamp of last Adaptive Card update (used by flows) |
| -- | `createdon` | `createdon` | Created On | DateTime | -- | Auto | Auto-set by Dataverse |
| -- | `modifiedon` | `modifiedon` | Modified On | DateTime | -- | Auto | Auto-set by Dataverse |

### Picklist Values for cr_status

**CRITICAL:** `cr_status` on this table is a **Picklist (Choice/OptionSet)** backed by integer values, NOT a string. This predates the conversations table and is referenced by multiple DV-triggered Power Automate flows with integer filters.

| Integer Value | Label | Description |
|---|---|---|
| 1 | Pending | Task created, waiting for worker pickup |
| 3 | Queued | Task claimed but dev box is busy; waiting for current task to finish |
| 5 | Running | Task is actively executing on the worker |
| 7 | Completed | Task finished successfully |
| 8 | Failed | Task failed (error or timeout) |
| 9 | Canceled | Task was canceled by user |

**Note on gaps:** Values 2 and 4 are intentionally skipped. Value 3 (Queued) was added later for multi-devbox scheduling. The Power Automate flows use these exact integer values in their trigger filters.

### Relationships

None (no formal Dataverse lookup relationships). Task ownership is tracked by `crb3b_useremail` (string), and dev box assignment by `crb3b_devbox` (string).

### Key Usage Patterns

- **Task creation:** Task manager POSTs row with `cr_status=1` (Pending), `crb3b_useremail`, `crb3b_devbox`, `crb3b_workingdir`
- **Atomic claiming:** Worker PATCHes `cr_status=5` (Running) using If-Match ETag header
- **Queuing:** If devbox busy, worker PATCHes `cr_status=3` (Queued) instead
- **Promotion:** When devbox frees up, oldest queued task is PATCHed back to `cr_status=1` (Pending)
- **Completion:** Worker PATCHes `cr_status=7` (Completed) + `cr_result` + `crb3b_sessionsummary`
- **Flow triggers:** TaskRunner triggers on row creation with `cr_status=1` (Pending), TaskCompleted on `cr_status=7`, TaskFailed on `cr_status=8`, TaskCanceled on `cr_status=9`

---

## 6. Table: cr_shragamessages

**Purpose:** Progress messages from the worker during task execution. Each row is a single activity log entry that appears on the Running card in Teams.

| Property | Value |
|---|---|
| Logical Name (entity) | `cr_shragamessage` |
| Logical Name (collection/plural) | `cr_shragamessages` |
| Display Name | Shraga Messages |
| Solution Publisher Prefix | `cr_` (base table), `crb3b_` (additional columns) |
| Primary Key Column | `cr_shragamessageid` (GUID, auto-generated) |
| Primary Name Column | `cr_name` |
| Ownership Type | User or Team |

### Columns

| # | Logical Name | Schema Name | Display Name | Data Type | Max Length | Required | Description |
|---|---|---|---|---|---|---|---|
| 1 | `cr_shragamessageid` | `cr_shragamessageid` | Shraga Message | Uniqueidentifier (GUID) | -- | Auto | Primary key, auto-generated |
| 2 | `cr_name` | `cr_name` | Name | Single Line of Text (String) | 450 | Required (Business Required) | First line of message, truncated to ~450 chars (Dataverse primary name column limit) |
| 3 | `cr_content` | `cr_content` | Content | Multiple Lines of Text (Memo) | 100000 | Optional | Full message content (tool call descriptions, progress text). No character limit |
| 4 | `cr_from` | `cr_from` | From | Single Line of Text (String) | 200 | Optional | Source identifier (e.g., `Shraga Worker`, `worker`, `verifier`) |
| 5 | `cr_to` | `cr_to` | To | Single Line of Text (String) | 200 | Optional | Target identifier (e.g., user email, `card`, `log`) |
| 6 | `crb3b_taskid` | `crb3b_TaskId` | Task ID | Single Line of Text (String) | 200 | Optional | Task ID reference (plain string, NOT a formal lookup). Used by TaskProgressUpdater flow to correlate messages with Running cards |
| -- | `createdon` | `createdon` | Created On | DateTime | -- | Auto | Auto-set by Dataverse. Used as timestamp for activity log ordering |
| -- | `modifiedon` | `modifiedon` | Modified On | DateTime | -- | Auto | Auto-set by Dataverse |

### Relationships

None (no formal Dataverse lookup relationships). The `crb3b_taskid` column stores a task GUID as a plain string for correlation.

### Key Usage Patterns

- **Progress reporting:** Worker POSTs a new row for each activity (tool call, reasoning step)
- **Card updates:** TaskProgressUpdater flow triggers on new row creation, reads `crb3b_taskid`, updates the Running card in Workflows chat
- **Activity log fetch:** Worker reads back all messages for a task to include in the session summary

---

## 7. Table: crb3b_shragausers

**Purpose:** Tracks per-user onboarding progress, dev box assignment, and manager status. The Global Shraga persists state here to survive restarts.

| Property | Value |
|---|---|
| Logical Name (entity) | `crb3b_shragauser` |
| Logical Name (collection/plural) | `crb3b_shragausers` |
| Display Name | Shraga Users |
| Solution Publisher Prefix | `crb3b_` |
| Primary Key Column | `crb3b_shragauserid` (GUID, auto-generated) |
| Primary Name Column | `crb3b_useremail` |
| Ownership Type | User or Team |

### Columns

| # | Logical Name | Schema Name | Display Name | Data Type | Max Length | Required | Description |
|---|---|---|---|---|---|---|---|
| 1 | `crb3b_shragauserid` | `crb3b_shragauserid` | Shraga User | Uniqueidentifier (GUID) | -- | Auto | Primary key, auto-generated |
| 2 | `crb3b_useremail` | `crb3b_useremail` | User Email | Single Line of Text (String) | 200 | Required (Business Required) | Primary identifier -- user's email address. Also serves as primary name column |
| 3 | `crb3b_azureadid` | `crb3b_azureadid` | Azure AD ID | Single Line of Text (String) | 200 | Optional | Azure AD object ID (used for DevCenter API calls) |
| 4 | `crb3b_devboxname` | `crb3b_devboxname` | DevBox Name | Single Line of Text (String) | 200 | Optional | Dev box name (e.g., `shraga-sagik`) |
| 5 | `crb3b_devboxstatus` | `crb3b_devboxstatus` | DevBox Status | Single Line of Text (String) | 50 | Optional | Provisioning state. Values: `Provisioning`, `Succeeded`, `Failed` |
| 6 | `crb3b_claudeauthstatus` | `crb3b_claudeauthstatus` | Claude Auth Status | Single Line of Text (String) | 50 | Optional | Authentication state. Values: `Pending`, `Authenticated`, `Failed` |
| 7 | `crb3b_managerstatus` | `crb3b_managerstatus` | Manager Status | Single Line of Text (String) | 50 | Optional | Personal manager state. Values: `Starting`, `Running`, `Offline` |
| 8 | `crb3b_onboardingstep` | `crb3b_onboardingstep` | Onboarding Step | Single Line of Text (String) | 100 | Optional | Current onboarding stage. Values: `awaiting_setup`, `provisioning`, `waiting_provisioning`, `provisioning_failed`, `auth_pending`, `auth_code_sent`, `completed` |
| 9 | `crb3b_lastseen` | `crb3b_lastseen` | Last Seen | DateTime | -- | Optional | Updated on every user interaction. ISO 8601 format |
| 10 | `crb3b_connectionurl` | `crb3b_connectionurl` | Connection URL | Single Line of Text (String) | 1000 | Optional | Dev box RDP/browser connection URL (e.g., `https://devbox.microsoft.com/connect?devbox={name}`) |
| 11 | `crb3b_authurl` | `crb3b_authurl` | Auth URL | Single Line of Text (String) | 1000 | Optional | Claude Code device authentication URL (sent to user for sign-in) |
| -- | `createdon` | `createdon` | Created On | DateTime | -- | Auto | Auto-set by Dataverse |
| -- | `modifiedon` | `modifiedon` | Modified On | DateTime | -- | Auto | Auto-set by Dataverse |

> **WARNING -- `crb3b_connectionurl` and `crb3b_authurl` may not exist:**
> These two columns (`crb3b_connectionurl`, `crb3b_authurl`) may not be present in
> all Dataverse environments. Some deployments omit them entirely, causing API
> responses to exclude these fields from the returned JSON. Code that reads user
> records MUST use safe access patterns (e.g., `.get("crb3b_connectionurl")` with a
> `None` default) and MUST NOT crash when these columns are absent. Additionally,
> these columns MUST NOT be included in PATCH or POST payloads -- writing to a
> non-existent column will cause a Dataverse API error. The connection URL should
> instead be obtained at runtime from the DevCenter API via `check_devbox_status`.

### String Values for crb3b_onboardingstep

| Value | Meaning |
|---|---|
| `awaiting_setup` | User record created, waiting for user to run setup.ps1 |
| `provisioning` | Dev box provisioning just started |
| `waiting_provisioning` | Waiting for dev box provisioning to complete |
| `provisioning_failed` | Dev box provisioning failed |
| `auth_pending` | Dev box ready, Claude auth not yet started |
| `auth_code_sent` | Auth URL sent to user, waiting for code |
| `completed` | Onboarding fully complete, user is operational |

### Relationships

None (no formal Dataverse lookup relationships).

### Key Usage Patterns

- **User lookup:** Global Shraga queries by `crb3b_useremail` to check onboarding state
- **State persistence:** On every onboarding step change, Global Shraga PATCHes the row
- **New user creation:** Global Shraga POSTs a new row when a user is first seen
- **Last seen tracking:** Updated on every user interaction via `crb3b_lastseen`

---

## 8. Picklist / Choice Definitions

Only one table uses a Picklist (Choice) column. All other "status" columns are plain strings.

### Task Status Choice (for cr_shraga_tasks.cr_status)

| Property | Value |
|---|---|
| Choice Name | Task Status (or `cr_status`) |
| Scope | Local (table-specific) |
| Data Type | Whole Number (Int32) |

| Value | Label |
|---|---|
| 1 | Pending |
| 3 | Queued |
| 5 | Running |
| 6 | Waiting for Input |
| 7 | Completed |
| 8 | Failed |
| 9 | Canceled |

**Note:** Values 2 and 4 are intentionally unused. Do NOT create options for them.

---

## 9. Row-Level Security Configuration

### Permission Model

| Role | Access |
|---|---|
| Admin (Sagi) | Read/write ALL rows in all tables |
| Regular users | Read/write ONLY their own rows -- no access to other users' rows at all |

### Implementation: Security Roles

Create two custom security roles:

#### Role 1: Shraga Admin

| Table | Create | Read | Write | Delete | Append | AppendTo |
|---|---|---|---|---|---|---|
| cr_shraga_conversations | Organization | Organization | Organization | Organization | Organization | Organization |
| cr_shraga_tasks | Organization | Organization | Organization | Organization | Organization | Organization |
| cr_shragamessages | Organization | Organization | Organization | Organization | Organization | Organization |
| crb3b_shragausers | Organization | Organization | Organization | Organization | Organization | Organization |

"Organization" scope means access to ALL rows regardless of owner.

#### Role 2: Shraga User

| Table | Create | Read | Write | Delete | Append | AppendTo |
|---|---|---|---|---|---|---|
| cr_shraga_conversations | User | User | User | None | User | User |
| cr_shraga_tasks | User | User | User | None | User | User |
| cr_shragamessages | User | User | User | None | User | User |
| crb3b_shragausers | User | User | User | None | User | User |

"User" scope means access ONLY to rows owned by the user.

### Important Notes on Row Ownership

- When Python code writes rows via the Dataverse Web API using `DefaultAzureCredential`, the **owning user** of each row is the authenticated identity
- For the Personal Task Manager: rows are created under the dev box user's identity, so `User` scope works correctly
- For the Global Shraga: rows are created under the service identity; ensure the GS's identity is the admin or uses the Shraga Admin role
- Power Automate flows create rows under the flow connection's identity; if flows need to read/write all rows, the connection identity needs the Shraga Admin role

### Configuration Steps in Power Apps

1. Navigate to **Settings** > **Security** > **Security Roles** in the Power Platform admin center
2. Create the two roles above
3. Assign **Shraga Admin** to the admin user (Sagi) and to service accounts (Global Shraga, Power Automate connections)
4. Assign **Shraga User** to all regular end users
5. Ensure table ownership type is **User or Team** (not Organization) for all four tables -- this enables row-level security

---

## 10. Step-by-Step Creation Instructions

### Step 1: Create Solution Publishers

1. Go to https://make.powerapps.com > select your environment
2. Navigate to **Solutions** > **New solution**
3. Click **New publisher**
4. Create Publisher 1:
   - Display Name: `Shraga` (or your preferred name)
   - Name: `shraga` (or your preferred name)
   - **Prefix: `cr`**
5. Create Publisher 2:
   - Display Name: `Shraga Extended` (or your preferred name)
   - Name: `shragaextended` (or your preferred name)
   - **Prefix: `crb3b`**

### Step 2: Create Solutions

1. Create Solution 1 (using Publisher 1 / `cr` prefix):
   - Display Name: `Shraga Core`
   - Publisher: (select the `cr` prefix publisher)
2. Create Solution 2 (using Publisher 2 / `crb3b` prefix):
   - Display Name: `Shraga Extended`
   - Publisher: (select the `crb3b` prefix publisher)

### Step 3: Create Table -- cr_shraga_conversations

Working in **Shraga Core** solution (cr prefix):

1. Click **New** > **Table** > **Table**
2. Display name: `Shraga Conversation`
3. Plural display name: `Shraga Conversations`
4. Primary column name: `Name` (this becomes `cr_name`, max length 100)
5. Click **Save**
6. Open the table and add custom columns one at a time:

| Order | Display Name | Data Type | Settings |
|---|---|---|---|
| 1 | User Email | Single Line of Text | Max length: 200 |
| 2 | MCS Conversation ID | Single Line of Text | Max length: 500 |
| 3 | Message | Multiple Lines of Text | Max length: 100000, Format: Text |
| 4 | Direction | Single Line of Text | Max length: 50 |
| 5 | Status | Single Line of Text | Max length: 50 |
| 6 | Claimed By | Single Line of Text | Max length: 200 |
| 7 | In Reply To | Single Line of Text | Max length: 100 |
| 8 | Follow-up Expected | Single Line of Text | Max length: 10 |

### Step 4: Create Table -- cr_shraga_tasks

Working in **Shraga Core** solution (cr prefix):

1. Click **New** > **Table** > **Table**
2. Display name: `Shraga Task`
3. Plural display name: `Shraga Tasks`
4. Primary column name: `Task ID` (this becomes `cr_name`, max length 100)
5. Click **Save**
6. Add custom columns:

**Phase A: cr_ prefix columns (in Shraga Core solution)**

| Order | Display Name | Data Type | Settings |
|---|---|---|---|
| 1 | Prompt | Multiple Lines of Text | Max length: 100000, Format: Text |
| 2 | Status | **Choice** | Create new choice. Name: `Task Status`. Add options with EXACT values: 1=Pending, 3=Queued, 5=Running, 6=Waiting for Input, 7=Completed, 8=Failed, 9=Canceled |
| 3 | Status Message | Single Line of Text | Max length: 200 |
| 4 | Result | Multiple Lines of Text | Max length: 100000, Format: Text |
| 5 | Transcript | Multiple Lines of Text | Max length: 1048576, Format: Text |
| 6 | User ID | Single Line of Text | Max length: 200 |

**Phase B: crb3b_ prefix columns (switch to Shraga Extended solution)**

Open the **Shraga Extended** solution, add the existing `Shraga Task` table to this solution, then add these columns:

| Order | Display Name | Data Type | Settings |
|---|---|---|---|
| 7 | User Email | Single Line of Text | Max length: 200 |
| 8 | Dev Box | Single Line of Text | Max length: 200 |
| 9 | Working Directory | Single Line of Text | Max length: 500 |
| 10 | Deep Link | Single Line of Text | Max length: 1000 |
| 11 | Running Chat ID | Single Line of Text | Max length: 500 |
| 12 | Running Message ID | Single Line of Text | Max length: 500 |
| 13 | OneDrive URL | Single Line of Text | Max length: 1000 |
| 14 | Session Summary | Multiple Lines of Text | Max length: 1048576, Format: Text |
| 15 | Chat ID | Single Line of Text | Max length: 500 |
| 16 | Conversation ID | Single Line of Text | Max length: 500 |
| 17 | Message ID | Single Line of Text | Max length: 500 |
| 18 | Control Chat ID | Single Line of Text | Max length: 500 |
| 19 | Control Message ID | Single Line of Text | Max length: 500 |
| 20 | Last Card Update Time | Date and Time | Behavior: User Local |

### Step 5: Create the Task Status Choice (Picklist)

When creating the Status column on cr_shraga_tasks, create a **local choice** with these exact integer values:

1. In the column creation dialog, select **Choice** as data type
2. Click **New choice**
3. Add each option. **CRITICAL: You must set the integer value explicitly for each option** -- do not accept auto-assigned values:
   - Click the "..." next to each option to set the value manually
   - Value `1` -> Label: `Pending`
   - Value `3` -> Label: `Queued`
   - Value `5` -> Label: `Running`
   - Value `6` -> Label: `Waiting for Input`
   - Value `7` -> Label: `Completed`
   - Value `8` -> Label: `Failed`
   - Value `9` -> Label: `Canceled`
4. Do NOT add options for values 2 or 4

**If the Power Apps UI does not allow setting specific integer values:**
Use the Dataverse Web API to create the option set:

```http
POST [org_url]/api/data/v9.2/EntityDefinitions(LogicalName='cr_shraga_task')/Attributes
Content-Type: application/json

{
  "@odata.type": "Microsoft.Dynamics.CRM.PicklistAttributeMetadata",
  "SchemaName": "cr_status",
  "DisplayName": { "@odata.type": "Microsoft.Dynamics.CRM.Label", "LocalizedLabels": [{ "Label": "Status", "LanguageCode": 1033 }] },
  "RequiredLevel": { "Value": "None" },
  "OptionSet": {
    "@odata.type": "Microsoft.Dynamics.CRM.OptionSetMetadata",
    "IsGlobal": false,
    "OptionSetType": "Picklist",
    "Options": [
      { "Value": 1, "Label": { "@odata.type": "Microsoft.Dynamics.CRM.Label", "LocalizedLabels": [{ "Label": "Pending", "LanguageCode": 1033 }] } },
      { "Value": 3, "Label": { "@odata.type": "Microsoft.Dynamics.CRM.Label", "LocalizedLabels": [{ "Label": "Queued", "LanguageCode": 1033 }] } },
      { "Value": 5, "Label": { "@odata.type": "Microsoft.Dynamics.CRM.Label", "LocalizedLabels": [{ "Label": "Running", "LanguageCode": 1033 }] } },
      { "Value": 6, "Label": { "@odata.type": "Microsoft.Dynamics.CRM.Label", "LocalizedLabels": [{ "Label": "Waiting for Input", "LanguageCode": 1033 }] } },
      { "Value": 7, "Label": { "@odata.type": "Microsoft.Dynamics.CRM.Label", "LocalizedLabels": [{ "Label": "Completed", "LanguageCode": 1033 }] } },
      { "Value": 8, "Label": { "@odata.type": "Microsoft.Dynamics.CRM.Label", "LocalizedLabels": [{ "Label": "Failed", "LanguageCode": 1033 }] } },
      { "Value": 9, "Label": { "@odata.type": "Microsoft.Dynamics.CRM.Label", "LocalizedLabels": [{ "Label": "Canceled", "LanguageCode": 1033 }] } }
    ]
  }
}
```

### Step 6: Create Table -- cr_shragamessages

Working in **Shraga Core** solution (cr prefix):

1. Click **New** > **Table** > **Table**
2. Display name: `Shraga Message`
3. Plural display name: `Shraga Messages`
4. Primary column name: `Name` (this becomes `cr_name`, max length 450)
5. Click **Save**
6. Add custom columns:

**Phase A: cr_ prefix columns (in Shraga Core solution)**

| Order | Display Name | Data Type | Settings |
|---|---|---|---|
| 1 | Content | Multiple Lines of Text | Max length: 100000, Format: Text |
| 2 | From | Single Line of Text | Max length: 200 |
| 3 | To | Single Line of Text | Max length: 200 |

**Phase B: crb3b_ prefix column (switch to Shraga Extended solution)**

| Order | Display Name | Data Type | Settings |
|---|---|---|---|
| 4 | Task ID | Single Line of Text | Max length: 200 |

### Step 7: Create Table -- crb3b_shragausers

Working in **Shraga Extended** solution (crb3b prefix):

1. Click **New** > **Table** > **Table**
2. Display name: `Shraga User`
3. Plural display name: `Shraga Users`
4. Primary column name: `User Email` (this becomes `crb3b_useremail`, max length 200)
5. Click **Save**
6. Add custom columns:

| Order | Display Name | Data Type | Settings |
|---|---|---|---|
| 1 | Azure AD ID | Single Line of Text | Max length: 200 |
| 2 | DevBox Name | Single Line of Text | Max length: 200 |
| 3 | DevBox Status | Single Line of Text | Max length: 50 |
| 4 | Claude Auth Status | Single Line of Text | Max length: 50 |
| 5 | Manager Status | Single Line of Text | Max length: 50 |
| 6 | Onboarding Step | Single Line of Text | Max length: 100 |
| 7 | Last Seen | Date and Time | Behavior: User Local |
| 8 | Connection URL | Single Line of Text | Max length: 1000 |
| 9 | Auth URL | Single Line of Text | Max length: 1000 |

### Step 8: Configure Security Roles

Follow the instructions in [Section 9](#9-row-level-security-configuration) to create and assign the Shraga Admin and Shraga User security roles.

### Step 9: Publish Customizations

1. In each solution, click **Publish all customizations**
2. Wait for the publish to complete
3. Verify tables appear in the Dataverse table list

---

## 11. Validation Checklist

After creating all tables, verify the following:

### Table Existence
- [ ] `cr_shraga_conversations` exists and is accessible via `[org_url]/api/data/v9.2/cr_shraga_conversations`
- [ ] `cr_shraga_tasks` exists and is accessible via `[org_url]/api/data/v9.2/cr_shraga_tasks`
- [ ] `cr_shragamessages` exists and is accessible via `[org_url]/api/data/v9.2/cr_shragamessages`
- [ ] `crb3b_shragausers` exists and is accessible via `[org_url]/api/data/v9.2/crb3b_shragausers`

### Column Verification (API Test)

Run these OData queries to verify columns exist:

```
GET [org_url]/api/data/v9.2/cr_shraga_conversations?$top=0&$select=cr_name,cr_useremail,cr_mcs_conversation_id,cr_message,cr_direction,cr_status,cr_claimed_by,cr_in_reply_to,cr_followup_expected
```

```
GET [org_url]/api/data/v9.2/cr_shraga_tasks?$top=0&$select=cr_name,cr_prompt,cr_status,cr_statusmessage,cr_result,cr_transcript,cr_userid,crb3b_useremail,crb3b_devbox,crb3b_workingdir,crb3b_deeplink,crb3b_runningchatid,crb3b_runningmessageid,crb3b_onedriveurl,crb3b_sessionsummary
```

```
GET [org_url]/api/data/v9.2/cr_shragamessages?$top=0&$select=cr_name,cr_content,cr_from,cr_to,crb3b_taskid
```

```
GET [org_url]/api/data/v9.2/crb3b_shragausers?$top=0&$select=crb3b_useremail,crb3b_azureadid,crb3b_devboxname,crb3b_devboxstatus,crb3b_claudeauthstatus,crb3b_managerstatus,crb3b_onboardingstep,crb3b_lastseen,crb3b_connectionurl,crb3b_authurl
```

Each query should return HTTP 200 with `"value": []`. If any column name causes an error, the column was not created correctly.

### Picklist Verification

```
GET [org_url]/api/data/v9.2/cr_shraga_tasks?$top=1&$select=cr_status
```

If a task row exists with `cr_status`, the value should be an integer (1, 3, 5, 6, 7, 8, or 9), not a string.

### Row-Level Security Test

1. Create a test row in `cr_shraga_conversations` as User A
2. Attempt to read it as User B (with Shraga User role)
3. User B should get zero results (row not visible)
4. Attempt to read it as Admin (with Shraga Admin role)
5. Admin should see the row

### Write/Create Test

```http
POST [org_url]/api/data/v9.2/cr_shraga_conversations
Content-Type: application/json

{
  "cr_name": "Schema validation test",
  "cr_useremail": "test@example.com",
  "cr_direction": "Inbound",
  "cr_status": "Unclaimed",
  "cr_message": "This is a test message"
}
```

Should return HTTP 204 (Created). Then clean up the test row.

---

## Appendix: Column Name Quick Reference

### cr_shraga_conversations
```
cr_shraga_conversationid, cr_name, cr_useremail, cr_mcs_conversation_id,
cr_message, cr_direction, cr_status, cr_claimed_by, cr_in_reply_to,
cr_followup_expected, createdon, modifiedon
```

### cr_shraga_tasks
```
cr_shraga_taskid, cr_name, cr_prompt, cr_status (Picklist!),
cr_statusmessage, cr_result, cr_transcript, cr_userid,
crb3b_useremail, crb3b_devbox, crb3b_workingdir, crb3b_deeplink,
crb3b_runningchatid, crb3b_runningmessageid, crb3b_onedriveurl,
crb3b_sessionsummary, crb3b_chatid, crb3b_conversationid,
crb3b_messageid, crb3b_controlchatid, crb3b_controlmessageid,
crb3b_lastcardupdatetime, createdon, modifiedon
```

### cr_shragamessages
```
cr_shragamessageid, cr_name, cr_content, cr_from, cr_to,
crb3b_taskid, createdon, modifiedon
```

### crb3b_shragausers
```
crb3b_shragauserid, crb3b_useremail, crb3b_azureadid,
crb3b_devboxname, crb3b_devboxstatus, crb3b_claudeauthstatus,
crb3b_managerstatus, crb3b_onboardingstep, crb3b_lastseen,
crb3b_connectionurl, crb3b_authurl, createdon, modifiedon
```
