---
description: Create a new matter in MatterMiner Core
---

1. Evaluate intent for matter creation (Keywords: "create matter", "new matter", "contract dispute").
2. Check if the user is authenticated; if not, present the login card.
3. Call `create_matter` with available details to initiate the intake/gating mechanism.
4. For required fields missing or in raw string form (client name, practice area, etc.):
   - Call respective lookup tool (e.g. `lookup_client`) to resolve to a system ID.
   - The system will naturally link the ID to the current draft.
5. Once all fields (`title`, `name`, `client_id`, `practice_area_id`, `description`, `case_stage_id`, `billing_type_id`) are confirmed, execute final `create_matter` call.
6. Present the final matter summary in a markdown table.
