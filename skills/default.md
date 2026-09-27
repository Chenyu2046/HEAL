<!-- version: 2 -->
<!-- risks: low,medium,high,unknown -->

# Default Repair Skill

- Treat the current source tree as the factual authority.
- Start with local context and expand only when the root-cause hypothesis needs it.
- Navigate in order: local context first (the warning's function and rule), then `search_code` with structured identifier queries, then `list_symbols` on the top matching file to pick the target function, then a targeted `read_file` of that range. Do not read whole files or jump between scattered line numbers.
- Do not turn an unresolved warning into an automatic suppression.
- A proposal is not a verified fix; verification requires an identity-matched result.
- Text search is navigation evidence, not proof that a C++ symbol has no callers.
