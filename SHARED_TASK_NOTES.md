# Shared Task Notes

## Status
New architecture per SHRAGA_SPEC_2026-02-15.md. Relay flow created FROM WITHIN Copilot Studio (resolves the previous "CloudFlow not found" blocker). Bot published with flow wired to Fallback topic.

## What's deployed
- **conversations table** (`cr_shraga_conversations`) — created and verified in Dataverse
- **Agent flow** (`68c91f74-85d8-0fec-b574-8ae9f315453b`) — created via CS "New Agent flow" button, published
  - Trigger: 3 text inputs (userEmail, conversationId, messageText)
  - Action: Add row to Shraga Conversations (direction=1/inbound, status=1/unclaimed, + user email, message text, MCS conversation ID from trigger inputs)
  - Response: default "Respond to the agent" (static ack)
  - NOTE: This is a "simple relay" — writes inbound message and returns immediately. Does NOT poll for outbound response.
- **Bot Fallback topic** (`928c6921-eb36-450f-b2bc-9ad966b3f02e`) — calls the agent flow on every unknown intent with:
  - userEmail = System.User.Email
  - conversationId = System.Conversation.Id
  - messageText = System.Activity.Text
  - Then sends static message: "I received your message. The task manager will process it shortly."
- **Conversational boosting** topic — disabled
- **Bot published** — 2/15/2026 ~7:10 PM

## Next steps (priority order)
1. **Test via Teams**: Send a message to the stam bot in Teams and verify the flow runs and creates a row in cr_shraga_conversations
2. **Upgrade flow to poll for response**: Replace the static ack with a Do Until loop that polls cr_shraga_conversations for an outbound response (direction=2, cr_in_reply_to = inbound row ID). This enables the full relay pattern where the bot waits for the task manager's response.
3. **Start Personal Shraga**: Run `task-manager/task_manager.py` to poll conversations and respond
4. **Start Global Shraga**: Run `global-manager/global_manager.py` as fallback
5. **Wire Global Shraga to DevBoxManager** for real user onboarding/provisioning
6. **Add deep link notifications** when tasks start running

## Key IDs
- Agent flow (CS-created): `68c91f74-85d8-0fec-b574-8ae9f315453b`
- Old ShragaRelayV2 flow: `09999803-760a-f111-8406-002248d570fd` (may still exist, not used)
- Old ShragaRelay flow (API-created, NOT usable from CS): `29f53538-6d0a-f111-8406-002248d570fd`
- Conversations table EntitySet: `cr_shraga_conversations` (display: "Shraga Conversations")
- Fallback topic component: `928c6921-eb36-450f-b2bc-9ad966b3f02e`
- Bot: `888e4800-5a06-f111-8406-7c1e5287291b` (schema: `copilots_header_97c4f`)

## Important Notes
- Spec: `Q:\sessions\shragaTest01\SHRAGA_SPEC_2026-02-15.md`
- Worker code: `Q:\repos\Users\sagik\shraga-worker`
- Dataverse: `https://org3e79cdb1.crm3.dynamics.com`
- Playwright available, kill Chrome first: `taskkill //F //IM chrome.exe`
- The flow was created through CS "New Agent flow" button in the topic editor. This is the ONLY way to make flows callable from CS topics. API-created flows cannot be resolved by CS's publish validator.
