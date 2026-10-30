# Speed up origin matching

- **ID:** 14290378561902239876
- **Repo:** github/expressjs/cors
- **Status:** AWAITING_USER_FEEDBACK
- **Created:** 2026-10-29T18:02:44Z
- **Completed:** 

## Prompt

Make isOriginAllowed faster for long origin lists.

## Latest plan

1. Benchmark isOriginAllowed
2. Use a Set for string origins

## Code outputs

- `patch-1.diff`: Use a Set for string origins (branch `jules/origin-set`)

## Activity

3 steps (full log in `activities.json`):

- agentActivity.planGenerated: 1
- agentActivity.runShell: 1
- userActivity.freeFormText: 1
