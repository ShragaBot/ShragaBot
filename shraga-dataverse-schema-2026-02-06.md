# Shraga Dataverse Schema Design

**Date:** February 6, 2026
**Purpose:** Data model for Shraga autonomous task execution system
**Environment:** Microsoft Dataverse

## Overview

Shraga uses 4 core tables to manage task execution, user metadata, worker infrastructure, and event streaming.

---

## Table 1: Tasks

**Table Name:** `cr5d6_cr_shraga_taskses`
**Purpose:** Stores task execution state and results
**Owner:** User who submitted the task

### Columns

| Column | Type | Required | Auto | Description |
|--------|------|----------|------|-------------|
| `id` | GUID | Yes | ✅ | Primary key |
| `name` | String | Yes | | Task identifier/name |
| `userid` | GUID | Yes | | Owner (links to Users table) |
| `status` | Integer | Yes | | Status code (1=pending, 5=running, 7=completed, 8=failed, 9=canceled) |
| `input` | JSON String | Yes | | Structured input: `{description, contactRules, successCriteria}` |
| `output` | JSON String | | | Results: `{summary, deliverables, verdict}` |
| `executionDetails` | JSON String | | | Execution metadata: `{devBoxUrl, sessionId, executionFolderPath, workerId}` |
| **`isMirror`** | Boolean | Yes | | True if this is admin's mirror copy |
| **`mirrorOfTaskId`** | GUID | | | If mirror: points to original user task |
| **`mirrorTaskId`** | GUID | | | If user task: points to admin mirror |
| **`assignedWorkerId`** | GUID | | | Worker assigned to execute this task |
| **`workerStatus`** | String | | | Worker's view: assigned, executing, done, failed |
| **`lastWorkerPing`** | DateTime | | | Worker heartbeat timestamp |
| `createdon` | DateTime | Yes | ✅ | When task was created |
| `modifiedon` | DateTime | Yes | ✅ | When task was last modified |
| `createdby` | User | Yes | ✅ | Who created (system) |
| `modifiedby` | User | Yes | ✅ | Who last modified (system) |

### Status Codes

- `1` - Pending (waiting for worker)
- `5` - Running (worker executing)
- `7` - Completed (successfully finished)
- `8` - Failed (execution error)
- `9` - Canceled (user canceled)

### Input JSON Structure
```json
{
  "description": "What to do",
  "contactRules": "When to reach out (e.g., on error, frequently, etc.)",
  "successCriteria": "How to verify completion"
}
```

### Output JSON Structure
```json
{
  "summary": "1-page summary of results",
  "deliverables": ["file1.md", "file2.py"],
  "verdict": {
    "approved": true,
    "testingDone": "What was tested",
    "results": "Test results"
  }
}
```

### ExecutionDetails JSON Structure
```json
{
  "devBoxUrl": "https://...",
  "sessionId": "abc-123-def",
  "executionFolderPath": "/shared/shraga-tasks/task-123"
}
```

---

## Table 2: Events

**Table Name:** `cr5d6_cr_shraga_events`
**Purpose:** Stores progress events and messages for streaming to users
**Owner:** System

### Columns

| Column | Type | Required | Auto | Description |
|--------|------|----------|------|-------------|
| `id` | GUID | Yes | ✅ | Primary key |
| `UserId` | GUID | Yes | | Recipient user (links to Users table) |
| `eventType` | String | Yes | | Type of event (see below) |
| `content` | String | Yes | | Event content/message text |
| `status` | String | Yes | | Event status (pending, sent, delivered) |
| `TaskId` | GUID | | | (Optional) Related task ID |
| `createdon` | DateTime | Yes | ✅ | When event occurred |
| `createdby` | User | Yes | ✅ | Who created (system) |

### Event Types

**Current:**
- `AgentMessage` - Worker progress updates (💭 thinking messages)

**Future (planned):**
- `TaskStarted` - Task execution began
- `TaskCompleted` - Task finished successfully
- `TaskFailed` - Task failed
- `DevBoxProvisioned` - Dev Box ready
- `Error` - System error occurred
- `UserNotification` - General notification

### Status Values
- `pending` - Event created, not yet sent
- `sent` - Sent to Power Automate
- `delivered` - Delivered to Teams
- `read` - User saw the message
- `failed` - Delivery failed

---

## Table 3: Workers

**Table Name:** `cr5d6_cr_shraga_workers`
**Purpose:** Tracks execution environments (Dev Boxes, VMs, etc.)
**Owner:** User who owns the worker

### Columns

| Column | Type | Required | Auto | Description |
|--------|------|----------|------|-------------|
| `id` | GUID | Yes | ✅ | Primary key |
| `UserId` | GUID | Yes | | Owner (links to Users table) |
| `type` | String | Yes | | Worker type (see below) |
| `status` | String | Yes | | Provisioning status (see below) |
| `details` | JSON String | Yes | | Type-specific configuration |
| **`currentTaskId`** | GUID | | | Task currently executing (nullable) |
| **`lastSeen`** | DateTime | | | Last heartbeat from worker |
| `createdon` | DateTime | Yes | ✅ | When worker was provisioned |
| `modifiedon` | DateTime | Yes | ✅ | When worker was last updated |
| `createdby` | User | Yes | ✅ | Who created (system) |
| `modifiedby` | User | Yes | ✅ | Who last modified (system) |

### Worker Types

**Current:**
- `DevBox` - Azure Dev Box

**Future (extensible):**
- `LocalMachine` - User's local machine
- `CloudVM` - Azure VM
- `Container` - Docker container

### Status Values
- `provisioning` - Being created
- `ready` - Available for tasks
- `busy` - Currently executing a task
- `failed` - Provisioning failed
- `deprovisioned` - Removed/deleted

### Details JSON Structure

**For type = "DevBox":**
```json
{
  "url": "https://abc123.devbox.azure.com",
  "resourceId": "/subscriptions/.../devboxes/shraga-user1",
  "poolName": "shraga-pool",
  "region": "westus2",
  "customSettings": {
    "pythonVersion": "3.11",
    "claudeCodeVersion": "latest"
  }
}
```

**For type = "LocalMachine"** (future):
```json
{
  "hostname": "DESKTOP-ABC123",
  "ipAddress": "192.168.1.100",
  "sshKey": "ssh-rsa AAAA...",
  "platform": "windows"
}
```

---

## Table 4: Users

**Table Name:** `cr5d6_cr_shraga_users`
**Purpose:** Stores Shraga-specific user metadata
**Owner:** System

### Columns

| Column | Type | Required | Auto | Description |
|--------|------|----------|------|-------------|
| `id` | GUID | Yes | ✅ | Primary key |
| `UserId` | GUID | Yes | | Azure AD User ID (links to systemuser) |
| `email` | String | Yes | | User email address |
| `displayName` | String | Yes | | User display name |
| `role` | Choice | Yes | | User role in Shraga (see below) |
| `status` | Choice | Yes | | Account status (see below) |
| `createdon` | DateTime | Yes | ✅ | When user joined Shraga |
| `modifiedon` | DateTime | Yes | ✅ | When user was last updated |
| `createdby` | User | Yes | ✅ | Who created (system) |
| `modifiedby` | User | Yes | ✅ | Who last modified (system) |

### Role Values
- `designer` - Regular user (designers, PMs, etc.)
- `admin` - System administrator (can see all data)

### Status Values
- `active` - Can use Shraga
- `disabled` - Cannot use Shraga
- `pending` - Invitation sent, not yet activated

---

## Relationships

```
Users (1) ────┬───> (M) Tasks
              │
              ├───> (M) Events
              │
              └───> (M) Workers

Tasks (1) ────────> (M) Events (optional link)
```

### Relationship Details

1. **Users → Tasks** (1:M)
   - One user can submit many tasks
   - Tasks.userid → Users.id

2. **Users → Events** (1:M)
   - One user can receive many events
   - Events.UserId → Users.id

3. **Users → Workers** (1:M)
   - One user can have multiple workers (Dev Boxes, VMs, etc.)
   - Workers.UserId → Users.id

4. **Tasks → Events** (1:M, optional)
   - One task can generate many events
   - Events.TaskId → Tasks.id (nullable)

---

## Security Model

### Row-Level Security

**Shraga User Role:**
- Tasks: **User** level (own tasks only)
- Events: **User** level (own events only)
- Workers: **User** level (own workers only)
- Users: **User** level (own record only)

**Shraga Admin Role:**
- Tasks: **Organization** level (all tasks)
- Events: **Organization** level (all events)
- Workers: **Organization** level (all workers)
- Users: **Organization** level (all users)

---

## Implementation Notes

### Environment Variables (Worker)
```bash
DATAVERSE_URL=https://org5d6fdc01.crm.dynamics.com
TABLE_NAME=cr5d6_cr_shraga_taskses
WEBHOOK_URL=<power-automate-webhook>
WEBHOOK_USER=user@microsoft.com
```

### Naming Convention
- Solution prefix: `cr5d6_`
- Shraga prefix: `cr_shraga_`
- Full format: `cr5d6_cr_shraga_{tablename}`

### JSON Serialization
- All JSON fields use string type in Dataverse
- Serialize/deserialize on application side
- Max field size: 100,000 characters

---

## Migration Path

### Phase 1: Current State (Existing)
- ✅ Tasks table exists (partial schema)
- ✅ Messages table exists (will rename to Events)

### Phase 2: MVP Updates
1. Create Workers table
2. Create Users table
3. Update Tasks table schema
4. Rename Messages → Events
5. Set up security roles

### Phase 3: Data Migration
1. Migrate existing task data
2. Create user records for existing users
3. Create worker records for existing Dev Boxes
4. Update worker code to use new schema

---

## Change Log

- **2026-02-06**: Initial schema design
  - 4 tables: Tasks, Events, Workers, Users
  - JSON fields for structured data
  - Row-level security model
  - Extensible worker types
