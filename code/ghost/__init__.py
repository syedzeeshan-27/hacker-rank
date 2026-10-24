"""Ghost Data Still Exists - deletion-audit engine.

Package layout (who owns what):
  config.py     paths, config.json, .env handling            (orchestrator)
  models.py     shared dataclasses / vocabularies            (orchestrator - the contract)
  loaders.py    CSV loaders for dataset/                     (orchestrator)
  llm.py        Anthropic client wrapper, cache, usage ledger (orchestrator)
  identity.py   request email -> Subject with all identifiers (WS-C)
  intent.py     request_text -> deletion / not deletion       (WS-C)
  activity.py   system_activity.csv -> Observations           (WS-C)
  evidence/     every evidence file format -> Observations    (WS-B)
  state.py      Observations <= audit_at -> ResourceState     (WS-D)
  policy.py     deletion_rules.csv -> required treatment      (WS-D)
  planner.py    ResourceStates -> ordered Steps + status      (WS-D)
  writer.py     output.csv serialization                      (orchestrator)
"""
